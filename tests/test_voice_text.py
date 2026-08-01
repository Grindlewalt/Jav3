"""voice_text: the pure text layer under voice mode — sentence chunking of
streamed tokens, markdown-to-speakable sanitizing, spoken-prefix math, and
the cut-off annotations. No model, no I/O."""
from backend.voice_text import (CUTOFF_MARK, CUTOFF_NOTHING, MAX_CHUNK,
                                SpeechChunker, annotate_cutoff,
                                chunk_sentences, heard_upto_note,
                                spoken_fraction, tts_sanitize)


# ---- chunk_sentences ---------------------------------------------------------

def test_holds_short_fragments():
    chunks, rest = chunk_sentences("Hello there. ")
    assert chunks == [] and rest == "Hello there. "   # under MIN_CHUNK: wait


def test_splits_complete_sentences():
    text = ("The weather on Mars is thin and cold today. "
            "Dust storms are rising over the northern plains. And")
    chunks, rest = chunk_sentences(text)
    assert chunks == ["The weather on Mars is thin and cold today.",
                      "Dust storms are rising over the northern plains."]
    assert rest == "And"


def test_newline_always_cuts():
    chunks, rest = chunk_sentences("First line\nsecond")
    assert chunks == ["First line"] and rest == "second"


def test_waits_for_whitespace_after_period():
    # the '.' might be mid-number or mid-token — never cut without the space
    chunks, rest = chunk_sentences("This sentence is long enough to cut, version 3.")
    assert chunks == []


def test_abbreviations_do_not_cut():
    text = "I spoke with Dr. Smith about the results yesterday. He agreed."
    chunks, _ = chunk_sentences(text)
    assert chunks[0] == "I spoke with Dr. Smith about the results yesterday."


def test_force_cut_past_max():
    text = "word " * 80                     # 400 chars, no sentence end
    chunks, rest = chunk_sentences(text)
    assert chunks and all(len(c) <= MAX_CHUNK for c in chunks)
    assert not any(c.count("wo rd") for c in chunks)   # never mid-word


# ---- tts_sanitize -------------------------------------------------------------

def test_sanitize_strips_markdown():
    assert tts_sanitize("**Bold** and `code` and [a link](https://x.example)") == \
        "Bold and code and a link"
    assert tts_sanitize("- bullet item one") == "bullet item one"
    assert tts_sanitize("### Heading") == "Heading"
    assert tts_sanitize("| a | b |") == ""            # table rows are eyes-only
    assert tts_sanitize("see https://example.com/x?y=1 there") == "see a link there"


def test_speech_chunker_code_fence_collapses():
    ch = SpeechChunker()
    out = []
    for piece in ("Here is the fix.\n", "```python\n", "x = 1\n",
                  "print(x)\n", "```\n", "That should do it.\n"):
        out += ch.feed(piece)
    assert out == ["Here is the fix.", "(code omitted.)", "That should do it."]


def test_speech_chunker_flush_takes_the_tail():
    ch = SpeechChunker()
    assert ch.feed("The answer is forty-two") == []
    assert ch.flush() == ["The answer is forty-two"]
    assert ch.flush() == []


# ---- spoken-prefix math ---------------------------------------------------------

def test_spoken_fraction_word_boundary():
    text = "the quick brown fox jumps over the lazy dog"
    half = spoken_fraction(text, 500, 1000)
    assert half and text.startswith(half)
    assert not half.endswith(" ") and " " in text[len(half):][:2]  # snapped


def test_spoken_fraction_edges():
    assert spoken_fraction("hello world", 0, 1000) == ""
    assert spoken_fraction("hello world", 2000, 1000) == "hello world"
    assert spoken_fraction("hello world", 100, 0) == ""


def test_annotate_cutoff():
    assert annotate_cutoff("") == CUTOFF_NOTHING
    out = annotate_cutoff("The capital of France is")
    assert out.startswith("The capital of France is ")
    assert out.endswith(CUTOFF_MARK)


def test_heard_upto_note_tail():
    note = heard_upto_note("one two three " * 10)
    assert "…" in note and note.count("three") == 4    # last 12 words only
