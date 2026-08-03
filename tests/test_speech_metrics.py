"""
Tests for speech_metrics.py — pure logic, no Ollama/GPU/network needed,
so these run reliably in CI. Several of these are direct regression tests
for real bugs found during development (see DEVELOPMENT_HISTORY.md).
"""
import speech_metrics


class TestFilterToMainUser:
    def test_no_main_user_returns_transcript_unchanged(self, isolated_data_dir):
        transcript = {
            "duration": 10,
            "text": "hello",
            "segments": [{"start": 0, "end": 5, "text": "hello", "speaker": "SPEAKER_00"}],
        }
        result = speech_metrics.filter_to_main_user(transcript)
        assert result is transcript

    def test_main_user_not_in_this_recording_returns_unchanged(self, isolated_data_dir):
        import speaker_profiles
        speaker_profiles.add_profile("Eric", [0.1, 0.2, 0.3], is_main_user=True)

        transcript = {
            "duration": 10,
            "text": "hello",
            "segments": [{"start": 0, "end": 5, "text": "hello", "speaker": "SPEAKER_00"}],
        }
        result = speech_metrics.filter_to_main_user(transcript)
        assert result is transcript

    def test_main_user_present_filters_correctly(self, isolated_data_dir):
        import speaker_profiles
        speaker_profiles.add_profile("Eric", [0.1, 0.2, 0.3], is_main_user=True)

        transcript = {
            "duration": 100,
            "text": "irrelevant",
            "segments": [
                {"start": 0, "end": 10, "text": "hello there", "speaker": "Eric"},
                {"start": 10, "end": 90, "text": "a long monologue", "speaker": "SPEAKER_01"},
                {"start": 90, "end": 100, "text": "sounds good", "speaker": "Eric"},
            ],
        }
        result = speech_metrics.filter_to_main_user(transcript)

        assert result is not transcript
        assert len(result["segments"]) == 2
        assert all(s["speaker"] == "Eric" for s in result["segments"])
        assert result["duration"] == 20  # 10 + 10, just Eric's own speaking time
        assert result["text"] == "hello there sounds good"


class TestComputePauses:
    """Direct regression tests for #48: pause detection must not count a
    stretch where someone ELSE was talking as the main user's own pause."""

    def test_gap_filled_by_another_speaker_is_not_a_pause(self):
        filtered = {
            "duration": 240,
            "segments": [
                {"start": 0, "end": 5, "text": "hi", "speaker": "Eric"},
                {"start": 235, "end": 240, "text": "ok", "speaker": "Eric"},
            ],
        }
        all_segments = [
            {"start": 0, "end": 5, "text": "hi", "speaker": "Eric"},
            {"start": 6, "end": 233, "text": "a long monologue", "speaker": "SPEAKER_01"},
            {"start": 235, "end": 240, "text": "ok", "speaker": "Eric"},
        ]
        result = speech_metrics.compute_pauses(filtered, all_segments=all_segments)
        assert result["pause_count"] == 0

    def test_genuine_silence_still_counted(self):
        filtered = {
            "duration": 15,
            "segments": [
                {"start": 0, "end": 5, "text": "hi", "speaker": "Eric"},
                {"start": 10, "end": 15, "text": "um, ok", "speaker": "Eric"},
            ],
        }
        all_segments = filtered["segments"]  # no one else present in this gap at all
        result = speech_metrics.compute_pauses(filtered, all_segments=all_segments)
        assert result["pause_count"] == 1
        assert result["longest_pause_seconds"] == 5

    def test_partial_overlap_at_gap_boundaries_still_excluded(self):
        filtered = {
            "duration": 25,
            "segments": [
                {"start": 0, "end": 5, "text": "hi", "speaker": "Eric"},
                {"start": 20, "end": 25, "text": "ok", "speaker": "Eric"},
            ],
        }
        all_segments = [
            {"start": 0, "end": 5, "text": "hi", "speaker": "Eric"},
            {"start": 3, "end": 8, "text": "overlap at start", "speaker": "SPEAKER_01"},
            {"start": 18, "end": 22, "text": "overlap at end", "speaker": "SPEAKER_01"},
            {"start": 20, "end": 25, "text": "ok", "speaker": "Eric"},
        ]
        result = speech_metrics.compute_pauses(filtered, all_segments=all_segments)
        assert result["pause_count"] == 0

    def test_no_all_segments_falls_back_to_original_behavior(self):
        """Backward compatibility: without all_segments, every gap counts,
        matching the pre-#48 behavior for single-speaker transcripts."""
        transcript = {
            "duration": 15,
            "segments": [
                {"start": 0, "end": 5, "text": "hi", "speaker": "SPEAKER_00"},
                {"start": 10, "end": 15, "text": "ok", "speaker": "SPEAKER_00"},
            ],
        }
        result = speech_metrics.compute_pauses(transcript)
        assert result["pause_count"] == 1


class TestComputePace:
    def test_basic_wpm_calculation(self):
        transcript = {"text": " ".join(["word"] * 140), "duration": 60}
        result = speech_metrics.compute_pace(transcript)
        assert result["word_count"] == 140
        assert result["words_per_minute"] == 140.0

    def test_zero_duration_does_not_crash(self):
        transcript = {"text": "hello world", "duration": 0}
        result = speech_metrics.compute_pace(transcript)
        assert result["words_per_minute"] == 0


class TestComputeFillerWords:
    def test_detects_known_filler_phrases(self):
        transcript = {"text": "so, like, I mean, I think we should, you know, go"}
        result = speech_metrics.compute_filler_words(transcript)
        assert result["total_filler_count"] > 0

    def test_no_fillers_present(self):
        transcript = {"text": "we should ship this today"}
        result = speech_metrics.compute_filler_words(transcript)
        assert result["total_filler_count"] == 0
