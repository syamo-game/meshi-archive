from services.page_evidence import (
    extract_restaurant_page_evidence,
    prove_page_candidate,
)
from services.resolution import CandidateIdentity


PAGE_URL = "https://restaurant.example/shops/alpha"


def candidate(**updates: object) -> CandidateIdentity:
    values: dict[str, object] = {
        "name": "Cafe Alpha",
        "area": "銀座",
        "address": "〒104-0061 東京都中央区銀座1-2-3",
        "phone": "03-1234-5678",
        "canonical_url": PAGE_URL,
        "evidence_url": PAGE_URL,
    }
    values.update(updates)
    return CandidateIdentity.model_validate(values)


def test_proves_one_structured_restaurant_node() -> None:
    html = """
    <html><head><script type="application/ld+json">
    {
      "@type": "Restaurant",
      "name": "Cafe Alpha",
      "address": "東京都中央区銀座1丁目2番地3号",
      "telephone": "03-1234-5678",
      "url": "https://restaurant.example/shops/alpha"
    }
    </script></head><body></body></html>
    """

    proof = prove_page_candidate(
        candidate(),
        extract_restaurant_page_evidence(html, PAGE_URL),
    )

    assert proof is not None
    assert proof.method == "structured_data"
    assert proof.candidate.phone == "03-1234-5678"


def test_rejects_page_with_multiple_restaurant_nodes() -> None:
    html = """
    <script type="application/ld+json">
    [
      {"@type":"Restaurant","name":"Cafe Alpha","address":"東京都中央区銀座1-2-3"},
      {"@type":"Restaurant","name":"Cafe Beta","address":"東京都中央区銀座9-9-9"}
    ]
    </script>
    """

    proof = prove_page_candidate(
        candidate(),
        extract_restaurant_page_evidence(html, PAGE_URL),
    )

    assert proof is None


def test_proves_visible_name_and_full_address_in_one_text_block() -> None:
    html = """
    <html><head><title>Cafe Alpha | 公式サイト</title></head>
    <body><p>Cafe Alpha 〒104-0061 東京都中央区銀座1丁目2番地3号</p></body></html>
    """

    proof = prove_page_candidate(
        candidate(),
        extract_restaurant_page_evidence(html, PAGE_URL),
    )

    assert proof is not None
    assert proof.method == "visible_page"
    assert proof.candidate.phone is None


def test_rejects_visible_heading_and_address_in_separate_blocks() -> None:
    html = """
    <title>Cafe Alpha</title>
    <p>東京都中央区銀座1-2-3</p>
    """

    proof = prove_page_candidate(
        candidate(),
        extract_restaurant_page_evidence(html, PAGE_URL),
    )

    assert proof is None


def test_rejects_name_or_address_found_only_in_script() -> None:
    html = """
    <html><head><title>店舗一覧</title></head>
    <body><script>Cafe Alpha 東京都中央区銀座1-2-3</script></body></html>
    """

    proof = prove_page_candidate(
        candidate(),
        extract_restaurant_page_evidence(html, PAGE_URL),
    )

    assert proof is None


def test_rejects_name_only_or_address_only() -> None:
    name_only = "<title>Cafe Alpha</title><body>住所はお問い合わせください</body>"
    address_only = "<title>店舗情報</title><body>東京都中央区銀座1-2-3</body>"

    assert prove_page_candidate(
        candidate(),
        extract_restaurant_page_evidence(name_only, PAGE_URL),
    ) is None
    assert prove_page_candidate(
        candidate(),
        extract_restaurant_page_evidence(address_only, PAGE_URL),
    ) is None


def test_visible_page_does_not_reuse_unverified_phone_or_category() -> None:
    html = """
    <meta property="og:title" content="Cafe Alpha">
    <body>Cafe Alpha 東京都中央区銀座1-2-3</body>
    """

    proof = prove_page_candidate(
        candidate(
            category="カフェ・喫茶店",
            external_source="tabelog",
            external_id="wrong-page-id",
        ),
        extract_restaurant_page_evidence(html, PAGE_URL),
    )

    assert proof is not None
    assert proof.candidate.phone is None
    assert proof.candidate.category is None
    assert proof.candidate.external_source is None
    assert proof.candidate.external_id is None


def test_rejects_visible_listing_page_with_multiple_addresses() -> None:
    html = """
    <title>Cafe Alpha 店舗一覧</title>
    <section><h2>銀座店</h2><p>東京都中央区銀座1-2-3</p></section>
    <section><h2>新宿店</h2><p>東京都新宿区新宿9-9-9</p></section>
    """

    proof = prove_page_candidate(
        candidate(),
        extract_restaurant_page_evidence(html, PAGE_URL),
    )

    assert proof is None


def test_rejects_visible_page_with_address_prefix_only() -> None:
    html = """
    <title>Cafe Alpha</title>
    <p>東京都中央区銀座1-2-30</p>
    """

    proof = prove_page_candidate(
        candidate(),
        extract_restaurant_page_evidence(html, PAGE_URL),
    )

    assert proof is None


def test_rejects_visible_page_with_name_prefix_only() -> None:
    html = """
    <title>Cafe Alphabet</title>
    <p>東京都中央区銀座1-2-3</p>
    """

    proof = prove_page_candidate(
        candidate(),
        extract_restaurant_page_evidence(html, PAGE_URL),
    )

    assert proof is None


def test_rejects_visible_page_with_japanese_name_prefix_only() -> None:
    html = """
    <title>花月</title>
    <p>東京都中央区銀座1-2-3</p>
    """

    proof = prove_page_candidate(
        candidate(name="花"),
        extract_restaurant_page_evidence(html, PAGE_URL),
    )

    assert proof is None


def test_rejects_listing_with_multiple_addresses_in_one_text_block() -> None:
    html = """
    <body>Cafe Alpha 店舗一覧。東京都新宿区新宿9-9-9。東京都中央区銀座1-2-3</body>
    """

    proof = prove_page_candidate(
        candidate(),
        extract_restaurant_page_evidence(html, PAGE_URL),
    )

    assert proof is None


def test_rejects_search_heading_combined_with_another_shops_address() -> None:
    html = """
    <title>Cafe Alpha の検索結果</title>
    <div>Cafe Beta</div>
    <div>東京都中央区銀座1-2-3</div>
    """

    proof = prove_page_candidate(
        candidate(),
        extract_restaurant_page_evidence(html, PAGE_URL),
    )

    assert proof is None
