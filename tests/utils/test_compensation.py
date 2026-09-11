from utils.compensation import extract_compensation


def test_waas_salary_and_equity_lines():
    text = (
        "Build the future of payments.\n"
        "Salary: $125K - $200K\n"
        "Equity: 0.25% - 2.00%\n"
        "Skills: Python, React"
    )
    comp = extract_compensation(text)
    assert comp.salary == "$125K - $200K"
    assert comp.equity == "0.25% - 2.00%"


def test_remote100k_leading_usd_range():
    text = "USD150,000–USD250,000\n\nWe are hiring a backend engineer."
    comp = extract_compensation(text)
    assert comp.salary == "USD150,000–USD250,000"
    assert comp.equity is None


def test_collapsed_description_still_reads_leading_range():
    text = "USD150,000–USD250,000 We are hiring a backend engineer in a global team."
    assert extract_compensation(text).salary == "USD150,000–USD250,000"


def test_html_salary_line():
    html = "<p>Salary: $140K - $180K</p><p>Equity: 0.10% - 0.50%</p>"
    comp = extract_compensation(html)
    assert comp.salary == "$140K - $180K"
    assert comp.equity == "0.10% - 0.50%"


def test_salary_range_in_sentence():
    text = "The salary range for this role is $140,000 to $180,000 annually."
    assert extract_compensation(text).salary == "$140,000 to $180,000"


def test_competitive_when_no_numbers():
    text = "Salary: Competitive\nGreat benefits."
    assert extract_compensation(text).salary == "Competitive"


def test_no_false_positive_on_gift_card():
    text = "We are a remote-first team. New hires receive a $50 Amazon gift card."
    comp = extract_compensation(text)
    assert comp.salary is None
    assert comp.equity is None


def test_collapsed_waas_one_liner_salary_skills():
    text = (
        "We make sure every factory in the world builds without mistakes. "
        "Salary: $150K - $200K Skills: Python, React"
    )
    comp = extract_compensation(text)
    assert comp.salary == "$150K - $200K"
    assert comp.equity is None


def test_collapsed_yc_salary_and_equity():
    text = (
        "AI spacecraft operators that lives onboard the vehicle "
        "Salary: $100K - $200K Equity: 0.25% - 2.00% Skills: C++, Python, Linux"
    )
    comp = extract_compensation(text)
    assert comp.salary == "$100K - $200K"
    assert comp.equity == "0.25% - 2.00%"


def test_empty_description():
    assert extract_compensation("") == (None, None)
    assert extract_compensation(None) == (None, None)
