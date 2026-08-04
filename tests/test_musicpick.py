"""The deterministic track picker.

Its job is to keep "play kick start my heart" to one tool call. Anything it gets
wrong costs a wrong song; anything it refuses to decide costs one extra turn. So
the tests are mostly about which side of that line each case falls on.
"""
import pytest

from backend.musicpick import Candidate, choose, describe, normalise, rank, score


def C(title, artist="", album="", source="tarmac", ref="1"):
    return Candidate(source=source, ref=ref, title=title, artist=artist, album=album)


# --- normalising --------------------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    ("Kick Start My Heart", "kick start my heart"),
    ("Kickstart My Heart (Remastered 2021)", "kickstart my heart"),
    ("Kickstart My Heart [Official Audio]", "kickstart my heart"),
    ("03 - Dr. Feelgood.mp3", "dr feelgood mp3"),
    ("Sk8er Boi", "sk8er boi"),
    ("Café del Mar", "cafe del mar"),
    ("AC/DC — Back In Black", "ac dc back in black"),
    ("Simon & Garfunkel", "simon and garfunkel"),
    ("  MESSY   spacing  ", "messy spacing"),
])
def test_titles_fold_to_comparable_words(raw, want):
    assert normalise(raw) == want


# --- the confident cases: one call, no asking ---------------------------------

def test_an_exact_title_wins_outright():
    cands = [C("Kickstart My Heart", "Mötley Crüe"),
             C("Home Sweet Home", "Mötley Crüe"),
             C("Heart Of Glass", "Blondie")]
    win, _, why = choose("kickstart my heart", cands)
    assert win and win.title == "Kickstart My Heart"
    assert why == "confident"


def test_decoration_on_the_library_side_does_not_break_it():
    cands = [C("Kickstart My Heart (Remastered 2021) [Official Audio]", "Mötley Crüe"),
             C("Dr. Feelgood", "Mötley Crüe")]
    win, _, _ = choose("kickstart my heart", cands)
    assert win and win.title.startswith("Kickstart")


def test_a_query_can_name_the_artist_instead():
    cands = [C("Nightcall", "Kavinsky", "OutRun"),
             C("Uptown Funk", "Mark Ronson")]
    win, _, _ = choose("kavinsky nightcall", cands)
    assert win and win.title == "Nightcall"


def test_a_filename_on_disk_matches_too():
    cands = [C("03 - Dr. Feelgood", source="local", ref="/m/03 - Dr. Feelgood.mp3"),
             C("Kickstart My Heart", source="local", ref="/m/k.mp3")]
    win, _, _ = choose("dr feelgood", cands)
    assert win and win.source == "local"


def test_a_near_miss_spelling_still_lands():
    cands = [C("Kickstart My Heart", "Mötley Crüe"), C("Sweet Child O' Mine", "GNR")]
    win, _, _ = choose("kick start my heart", cands)
    assert win and win.title == "Kickstart My Heart"


# --- the cases where asking is correct ---------------------------------------

def test_two_recordings_of_the_same_song_are_not_guessed_between():
    """Picking one silently is how the wrong version gets played."""
    cands = [C("Take On Me", "a-ha"), C("Take On Me", "MTV Unplugged")]
    win, shortlist, why = choose("take on me", cands)
    assert win is None
    assert why == "several recordings of the same song"
    assert len(shortlist) == 2


# --- the cases where asking is a refusal --------------------------------------

def test_an_artist_with_several_tracks_plays_one_instead_of_asking():
    """Every tied candidate answers what was asked, so stopping to ask is just
    a refusal. This is what made "play some Zach Bryan" play nothing at all —
    8 of his tracks in a dead heat, no winner, and 12 of the operator's 30
    tracks unreachable by artist name."""
    cands = [C("Pink Skies", "Zach Bryan"), C("28", "Zach Bryan"),
             C("Revival", "Zach Bryan"), C("Hey Driver", "Zach Bryan")]
    win, _shortlist, why = choose("zach bryan", cands)
    assert win is not None
    assert why == "confident"
    assert win.artist == "Zach Bryan"


def test_a_tie_across_different_songs_is_not_ambiguous():
    cands = [C("We Will Rock You", "Queen"), C("Another One Bites the Dust", "Queen")]
    win, _shortlist, why = choose("queen", cands)
    assert win is not None and why == "confident"


def test_same_title_by_different_artists_still_asks():
    """Squashed titles match, so this is the duplicate case even though the
    performers differ — a cover is exactly the wrong-version risk."""
    cands = [C("Hurt", "Nine Inch Nails"), C("Hurt", "Johnny Cash")]
    win, _shortlist, why = choose("hurt", cands)
    assert win is None and why == "several recordings of the same song"


def test_nothing_close_returns_no_winner():
    cands = [C("Nightcall", "Kavinsky"), C("Uptown Funk", "Mark Ronson")]
    win, shortlist, why = choose("bohemian rhapsody", cands)
    assert win is None
    assert why == "no confident match"
    assert shortlist, "the caller still needs something to show"


def test_an_empty_library_says_nothing_matched():
    win, shortlist, why = choose("anything", [])
    assert win is None and shortlist == [] and why == "nothing matched"


def test_an_empty_query_never_wins():
    assert score("", C("Anything")) == 0
    win, _, _ = choose("", [C("Anything")])
    assert win is None


# --- properties that keep it safe to act on ----------------------------------

def test_the_same_query_always_gives_the_same_answer():
    """Determinism is what makes it safe to play without confirming."""
    cands = [C("A Song", "X"), C("A Song", "Y"), C("Another", "Z")]
    first = [(s, c.ref) for s, c in rank("a song", cands)]
    for _ in range(20):
        assert [(s, c.ref) for s, c in rank("a song", cands)] == first


def test_ties_break_stably_rather_than_by_dict_order():
    a = [C("Same", "One", source="local", ref="1"),
         C("Same", "Two", source="tarmac", ref="2")]
    assert [c.ref for _, c in rank("same", a)] == [c.ref for _, c in rank("same", list(reversed(a)))]


def test_a_longer_library_title_loses_to_a_tighter_one():
    cands = [C("Heart"), C("Heart Of Glass"), C("My Heart Will Go On Forever And Ever")]
    win, _, _ = choose("heart", cands)
    assert win and win.title == "Heart"


def test_describe_says_where_it_came_from():
    assert "[library]" in describe(C("X", source="tarmac"))
    assert "[on disk]" in describe(C("X", source="local"))
    assert "— Artist" in describe(C("X", "Artist"))


# --- spacing, which is the most common near-miss in music titles --------------

@pytest.mark.parametrize("query,title", [
    ("kick start my heart", "Kickstart My Heart"),   # the operator's own example
    ("kickstart my heart", "Kick Start My Heart"),   # and the reverse
    ("sk8erboi", "Sk8er Boi"),
    ("ac dc", "AC/DC"),
    ("thewho", "The Who"),
])
def test_a_space_in_the_wrong_place_still_matches(query, title):
    """Word-based comparison scored these as barely related — two of four query
    words absent from the title. Comparing without spaces makes them identical."""
    cands = [C(title, "Someone"), C("Something Entirely Different", "Other")]
    win, _, why = choose(query, cands)
    assert win and win.title == title, why


def test_squashing_does_not_make_everything_match_everything():
    """The looser comparison must not start claiming confidence it lacks."""
    cands = [C("Nightcall", "Kavinsky"), C("Uptown Funk", "Mark Ronson")]
    win, _, why = choose("bohemian rhapsody", cands)
    assert win is None, why
    win, _, _ = choose("stairway to heaven", cands)
    assert win is None


# --- "play some music" names no track ----------------------------------------

def _generic():
    import importlib.util
    from backend.config import settings
    spec = importlib.util.spec_from_file_location(
        "mp_handler", settings.tools_dir / "music_play" / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._generic_ask


@pytest.mark.parametrize("query,generic,tag", [
    ("play some music", True, ""),
    ("something", True, ""),
    ("a good song", True, ""),
    ("something upbeat", True, ""),
    # a genre word IS the tag, not a title fragment — matching the literal
    # word "fast" against titles finds nothing, which is how "put on something
    # fast" ended up returning the whole library
    ("something fast", True, "fast"),
    ("drive", True, "drive"),
    # a real name is never generic, however much filler surrounds it
    ("Zach Bryan", False, ""),
    ("play some Radiohead", False, ""),
    ("Thunderstruck", False, ""),
])
def test_generic_asks_are_told_apart_from_named_tracks(query, generic, tag):
    assert _generic()(query, "") == (generic, tag)


def test_an_explicit_tag_survives_a_generic_query():
    assert _generic()("something", "drive") == (True, "drive")
