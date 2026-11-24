from app.parsers.profiles.text import dedouble_line


def test_a_fully_doubled_line_is_undone():
    assert dedouble_line(["TToottaall", "4422,,666600..0000"]) == ["Total", "42,660.00"]


def test_doubled_looking_words_in_a_normal_line_stay():
    # "5500" alone looks doubled; the line isn't, so nothing changes.
    assert dedouble_line(["PAY", "5500", "RENT"]) == ["PAY", "5500", "RENT"]
    assert dedouble_line(["5500"]) == ["5500"]
    assert dedouble_line(["5500", "1122"]) == ["5500", "1122"]


def test_short_doubled_words_alone_stay():
    assert dedouble_line(["ll"]) == ["ll"]
