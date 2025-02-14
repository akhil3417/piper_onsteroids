import json
import logging
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import numpy as np
import onnxruntime
from piper_phonemize import phonemize_codepoints, phonemize_espeak, tashkeel_run

from .config import PhonemeType, PiperConfig
from .const import BOS, EOS, PAD
from .util import audio_float_to_int16
import struct

_LOGGER = logging.getLogger(__name__)


@dataclass
class PiperVoice:
    session: onnxruntime.InferenceSession
    config: PiperConfig
    global_time: int

    @staticmethod
    def load(
        model_path: Union[str, Path],
        config_path: Optional[Union[str, Path]] = None,
        use_cuda: bool = False,
    ) -> "PiperVoice":
        """Load an ONNX model and config."""
        if config_path is None:
            config_path = f"{model_path}.json"

        with open(config_path, "r", encoding="utf-8") as config_file:
            config_dict = json.load(config_file)

        providers: List[Union[str, Tuple[str, Dict[str, Any]]]]
        if use_cuda:
            providers = [
                (
                    "CUDAExecutionProvider",
                    {"cudnn_conv_algo_search": "HEURISTIC"},
                )
            ]
        else:
            providers = ["CPUExecutionProvider"]

        # set a seed ro reduce randomness (for debug)
        # onnxruntime.set_seed(0)

        return PiperVoice(
            config=PiperConfig.from_dict(config_dict),
            session=onnxruntime.InferenceSession(
                str(model_path),
                sess_options=onnxruntime.SessionOptions(),
                providers=providers,
            ),
            global_time = 0
        )

    def phonemize(self, text: str) -> List[List[str]]:
        """Text to phonemes grouped by sentence."""
        phonemes = []
        next_phoneme_input = False
        merge_phonemes = False
        if self.config.phoneme_type == PhonemeType.ESPEAK:
            import re
            regex = re.compile(r"(\[\[|\]\])")
            split_text = re.split(regex, text)
            for text_part in split_text:
                if text_part == "]]":
                    merge_phonemes = True
                    next_phoneme_input = False
                if text_part == "[[":
                    next_phoneme_input = True
                if text_part == "[[" or text_part == "]]":
                    # add pause
                    phonemes[-1].append(" ")
                    continue
                if next_phoneme_input:
                    # add raw phoneme input, always merged to last segment
                    sentences = text_part.split("\n")
                    phonemes[-1].extend(sentences[0])
                    for p in sentences[1:]:
                        phonemes.append(list(p))
                else:
                    if self.config.espeak_voice == "ar":
                        # Arabic diacritization
                        # https://github.com/mush42/libtashkeel/
                        text = tashkeel_run(text_part)
                    ps = phonemize_espeak(text_part, self.config.espeak_voice)
                    if merge_phonemes:
                        # merge first list of token
                        phonemes[-1].extend(ps[0])
                        #add remaining list (more sentences)
                        for p in ps[1:]:
                            phonemes.append(p)
                        merge_phonemes = False;
                    else:
                       phonemes.extend(ps)
        elif self.config.phoneme_type == PhonemeType.TEXT:
            phonemes.extend(phonemize_codepoints(text_part))
        else:
            raise ValueError(f"Unexpected phoneme type: {self.config.phoneme_type}")
        return phonemes

    def phonemes_to_ids(self, phonemes: List[str]) -> List[int]:
        """Phonemes to ids."""
        id_map = self.config.phoneme_id_map
        ids: List[int] = list(id_map[BOS])

        for phoneme in phonemes:
            if phoneme not in id_map:
                _LOGGER.warning("Missing phoneme from id map: %s", phoneme)
                continue

            ids.extend(id_map[phoneme])
            ids.extend(id_map[PAD])

        ids.extend(id_map[EOS])

        return ids

    def synthesize(
        self,
        text: str,
        wav_file: wave.Wave_write,
        phoneme_input: bool,
        speaker_id: Optional[int] = None,
        length_scale: Optional[float] = None,
        noise_scale: Optional[float] = None,
        noise_w: Optional[float] = None,
        sentence_silence: float = 0.0,
        alignment_data: Optional[list] = None
    ):
        """Synthesize WAV audio from text."""
        wav_file.setframerate(self.config.sample_rate)
        wav_file.setsampwidth(2)  # 16-bit
        wav_file.setnchannels(1)  # mono

        for audio_bytes in self.synthesize_stream_raw(
            text,
            phoneme_input=phoneme_input,
            speaker_id=speaker_id,
            length_scale=length_scale,
            noise_scale=noise_scale,
            noise_w=noise_w,
            sentence_silence=sentence_silence,
            alignment_data=alignment_data
        ):
            wav_file.writeframes(audio_bytes)

    def synthesize_stream_raw(
        self,
        text: str,
        phoneme_input: bool,
        speaker_id: Optional[int] = None,
        length_scale: Optional[float] = None,
        noise_scale: Optional[float] = None,
        noise_w: Optional[float] = None,
        sentence_silence: float = 0.0,
        alignment_data: Optional[list] = None
    ) -> Iterable[bytes]:
        """Synthesize raw audio per sentence from text."""
        if phoneme_input:
            sentence_phonemes = [list(text)]
        else:
            sentence_phonemes = self.phonemize(text)
        # 16-bit mono
        num_silence_samples = int(sentence_silence * self.config.sample_rate)
        silence_bytes = bytes(num_silence_samples * 2)

        fulltext = text.split(" ")
        for phonemes in sentence_phonemes:
            if alignment_data != None:
                sentence_length = 0
                word_length = []
                sentence_phonemes = []
                sentence_text = []
                word = []
                # split sentence in words by ' '
                for letter in phonemes:
                    if letter != ' ':
                        word.append(letter)
                    else:
                      sentence_phonemes.append(word)
                      word = []
                if (len(word) > 0):
                    sentence_phonemes.append(word)
                # create temp audio for words
                for wordphonemes in sentence_phonemes:
                    phoneme_ids = self.phonemes_to_ids(wordphonemes)
                    wordraw = self.synthesize_ids_to_raw(
                        phoneme_ids,
                        speaker_id=speaker_id,
                        length_scale=length_scale,
                        noise_scale=noise_scale,
                        noise_w=noise_w,
                    )
                    length = len(wordraw)
                    word = fulltext[0]
                    fulltext.pop(0)
                    sentence_text.append(word)
                    sentence_length += length
                    word_length.append(length)
            # create real audio
            phoneme_ids = self.phonemes_to_ids(phonemes)
            raw = self.synthesize_ids_to_raw(
                phoneme_ids,
                speaker_id=speaker_id,
                length_scale=length_scale,
                noise_scale=noise_scale,
                noise_w=noise_w,
            )
            if alignment_data != None:
                # fix length discrepancy
                start = 0
                end = 0
                is_start = True
                # detect "silence" at start and end
                for index in range(0, len(raw) - 2, 2):
                    a = struct.unpack('<h',raw[index:index + 2])[0]
                    if abs(a) > 1500:
                        if is_start:
                            start = index
                            is_start = False;
                        end = index
                # forward global time with found silence
                self.global_time = self.global_time + start / 2 / self.config.sample_rate
                # length correction factor, single word vs sentence
                correction_factor = ((end - start) / sentence_length) / (2 * self.config.sample_rate)
                for index, w in enumerate(word_length):
                    length = w * correction_factor
                    alignment_data.append({"word": sentence_text[index], "start": self.global_time, "end": self.global_time + length})
                    self.global_time += length
            yield raw + silence_bytes

    def synthesize_ids_to_raw(
        self,
        phoneme_ids: List[int],
        speaker_id: Optional[int] = None,
        length_scale: Optional[float] = None,
        noise_scale: Optional[float] = None,
        noise_w: Optional[float] = None,
    ) -> bytes:
        """Synthesize raw audio from phoneme ids."""
        if length_scale is None:
            length_scale = self.config.length_scale

        if noise_scale is None:
            noise_scale = self.config.noise_scale

        if noise_w is None:
            noise_w = self.config.noise_w

        phoneme_ids_array = np.expand_dims(np.array(phoneme_ids, dtype=np.int64), 0)
        phoneme_ids_lengths = np.array([phoneme_ids_array.shape[1]], dtype=np.int64)
        scales = np.array(
            [noise_scale, length_scale, noise_w],
            dtype=np.float32,
        )

        args = {
            "input": phoneme_ids_array,
            "input_lengths": phoneme_ids_lengths,
            "scales": scales
        }

        if self.config.num_speakers <= 1:
            speaker_id = None

        if (self.config.num_speakers > 1) and (speaker_id is None):
            # Default speaker
            speaker_id = 0

        if speaker_id is not None:
            sid = np.array([speaker_id], dtype=np.int64)
            args["sid"] = sid

        # Synthesize through Onnx
        audio = self.session.run(None, args, )[0].squeeze((0, 1))
        audio = audio_float_to_int16(audio.squeeze())
        return audio.tobytes()
