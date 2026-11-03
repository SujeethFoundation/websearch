from services.page_extract import extract_html, infer_description

META = ("The Sujeeth Foundation connects college students with professional mentors "
        "through a six-week Career Circle.")
HTML = f"""<html><head><title> About | Sujeeth </title>
<meta name="description" content="{META}"></head>
<body><nav>Home About Donate</nav><h1>About</h1>
<p>Career Circles meet weekly. Each circle pairs students with a mentor who works in their field.</p>
<footer>Copyright</footer></body></html>"""


def test_description_comes_from_the_page_meta_tag():
    page = extract_html(HTML)
    assert page["title"] == "About | Sujeeth"
    assert page["description"] == META
    assert page["description_source"] == "page_meta"
    assert "Donate" not in page["text"] and "Copyright" not in page["text"]


def test_without_meta_the_description_is_the_opening_sentences():
    page = extract_html(HTML.replace(f'<meta name="description" content="{META}">', ""))
    assert page["description_source"] == "page_text"
    assert page["description"].startswith("Each circle pairs students with a mentor")
    assert "About |" not in page["description"]


def test_short_or_boilerplate_meta_is_ignored():
    html = HTML.replace(META, "Home page")
    assert extract_html(html)["description_source"] == "page_text"


def test_description_is_capped_at_a_sentence_or_word_boundary():
    text = " ".join(f"Sentence number {i} says something useful about the program." for i in range(30))
    d = infer_description(text, limit=150)
    assert len(d) <= 151 and d.endswith((".", "…"))


def test_menu_and_cookie_fragments_are_skipped():
    text = "We use cookies to improve your experience on this website today. The program matches students with mentors."
    assert infer_description(text) == "The program matches students with mentors."


def test_empty_text_gives_no_description():
    assert infer_description("") == ""
