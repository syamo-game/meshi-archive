from pathlib import Path


PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
USER_FACING_SOURCES: tuple[Path, ...] = (
    PROJECT_ROOT / "bot" / "discord_bot.py",
    PROJECT_ROOT / "bot" / "sync_logic.py",
    PROJECT_ROOT / "web" / "read_only.py",
    PROJECT_ROOT / "web" / "templates" / "_place_detail_summary.html",
    PROJECT_ROOT / "web" / "templates" / "_shop_cards.html",
    PROJECT_ROOT / "web" / "templates" / "admin.html",
    PROJECT_ROOT / "web" / "templates" / "base.html",
    PROJECT_ROOT / "web" / "templates" / "explore.html",
    PROJECT_ROOT / "web" / "templates" / "review.html",
    PROJECT_ROOT / "web" / "templates" / "shop.html",
)


def test_user_facing_sources_use_data_review_label() -> None:
    text: str = "\n".join(path.read_text(encoding="utf-8") for path in USER_FACING_SOURCES)

    assert "データ確認" in text
    assert "要確認" not in text
    assert "精査" not in text


def test_read_only_message_describes_the_current_mode() -> None:
    text: str = (PROJECT_ROOT / "web" / "read_only.py").read_text(encoding="utf-8")

    assert "読取専用モード" in text
    assert "データ再構築中" not in text
