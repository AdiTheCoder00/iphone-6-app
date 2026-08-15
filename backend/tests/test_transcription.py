"""Silence-artefact filtering for tap-to-talk transcripts."""

from app.services.transcription import _ARTEFACTS_NORMALIZED, _normalize


def test_silence_artefacts_match_despite_casing_and_punctuation():
    assert _normalize("Thank you!") in _ARTEFACTS_NORMALIZED
    assert _normalize("THANKS FOR WATCHING") in _ARTEFACTS_NORMALIZED
    assert _normalize("thank you.") in _ARTEFACTS_NORMALIZED
    assert _normalize("...") in _ARTEFACTS_NORMALIZED


def test_real_speech_is_not_a_silence_artefact():
    assert _normalize("thank you for the update, bye.") not in _ARTEFACTS_NORMALIZED
    assert _normalize("thanks for watching the plants") not in _ARTEFACTS_NORMALIZED