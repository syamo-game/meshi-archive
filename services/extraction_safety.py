from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol


SubjectKind = Literal["restaurant", "product", "event", "unknown"]
IdentityEvidence = Literal["explicit", "ambiguous", "author_only", "unknown"]
OperatingStatus = Literal["open", "closed", "event_ended", "unknown"]
EVENT_EXCLUDED = "event_excluded"


class EvidenceCode(StrEnum):
    INPUT_INCOMPLETE = "input_incomplete"
    IDENTITY_UNCLEAR = "identity_unclear"
    NAME_UNGROUNDED = "name_ungrounded"
    BRANCH_UNGROUNDED = "branch_ungrounded"
    NON_STORE_SUBJECT = "non_store_subject"
    EVENT_EXCLUDED = EVENT_EXCLUDED


@dataclass(frozen=True)
class EvidenceAssessment:
    codes: tuple[EvidenceCode, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def requires_review(self) -> bool:
        return bool(self.codes)

    @property
    def is_event_excluded(self) -> bool:
        return EvidenceCode.EVENT_EXCLUDED in self.codes


class MentionEvidence(Protocol):
    shop_name: str
    branch_name: str | None
    subject_kind: SubjectKind | None
    identity_evidence: IdentityEvidence | None
    name_evidence: str | None
    branch_evidence: str | None
    operating_status: OperatingStatus | None
    operating_status_evidence: str | None


class RegistrationState(Protocol):
    difference_type: str | None
    shop_id: int | None
    review_status: str


def is_event_excluded_registration(mention: RegistrationState) -> bool:
    return (
        mention.difference_type == EVENT_EXCLUDED
        and mention.shop_id is None
        and mention.review_status == "rejected"
    )


_TRUNCATION_MARKER = re.compile(
    r"(?:続きを読む|続きを(?:読む|見る)|(?:…|\.\.\.)\s*続き(?:はこちら)?)\s*[）)」]*\s*$",
    re.MULTILINE,
)
_AUTHOR_LINE = re.compile(r"^\[Embed Author\]|^\[Embed Title\].*(?:@|\bon [Xx]\b)")
_ERROR_PREFIX = "evidence_review:"
_NAME_CHARACTERS = "0-9a-zぁ-んァ-ヶ一-龠ー"


def source_requires_fresh_extraction(content: str) -> bool:
    return bool(re.search(r"商品名|舟盛り|催事|物産展|期間限定|ポップアップ|pop.?up|出店|閉店|営業終了", content, re.I))


def _explicit_non_store_subject(name: str, content: str) -> SubjectKind | None:
    quoted_name = re.escape(_normalized(name))
    text = _normalized(content)
    product = (
        rf"(?:商品名|舟盛り(?:商品)?|メニュー名)\s*(?:は|[:：])?\s*[「『]{quoted_name}[」』]"
        rf"|[「『]{quoted_name}[」』]\s*(?:という|は|の)?\s*(?:舟盛り|商品)"
    )
    event = (
        rf"[「『]?{quoted_name}[」』]?\s*(?:が|の|は)?\s*(?:期間限定出店|催事出店|物産展出店)"
        rf"|[「『]?{quoted_name}[」』]?(?:が|は|の)[^。！\n]{{0,45}}(?:催事|物産展)[^。！\n]{{0,15}}出店"
    )
    if re.search(product, text):
        return "product"
    if re.search(event, text):
        return "event"
    return None


def image_evidence_assessment(
    subject_kind: SubjectKind,
    *,
    content: str = "",
    unresolved_reason: str = "",
    input_assessment: EvidenceAssessment = EvidenceAssessment(),
) -> EvidenceAssessment:
    codes = list(input_assessment.codes)
    reasons = list(input_assessment.reasons)
    context = f"{content}\n{unresolved_reason}"
    if re.search(r"(?:期間限定)?(?:催事|物産展)(?:の(?:紹介|チラシ)|紹介|出店)|期間限定出店", context):
        subject_kind = "event"
    elif re.search(r"(?:商品|舟盛り)(?:の(?:紹介|チラシ)|紹介)", context):
        subject_kind = "product"
    if subject_kind in {"product", "event"}:
        codes.append(EvidenceCode.NON_STORE_SUBJECT)
        if subject_kind == "event":
            codes.append(EvidenceCode.EVENT_EXCLUDED)
        reasons.append(_non_store_reason(subject_kind))
    elif subject_kind != "restaurant":
        codes.append(EvidenceCode.IDENTITY_UNCLEAR)
        reasons.append("画像の店名と商品・催事の区別が未確認です。対象店舗を確認してください。")
    return EvidenceAssessment(tuple(dict.fromkeys(codes)), tuple(dict.fromkeys(reasons)))


def _non_store_reason(subject_kind: SubjectKind) -> str:
    if subject_kind == "event":
        return "催事は登録対象外です。出店元の常設店舗を特定できないため、会場や仮設支店は登録しません。"
    return "商品への言及を常設店舗として自動登録しません。販売店との関係を確認してください。"


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


def _contains_name(name: str, text: str) -> bool:
    compact_name = re.sub(r"[^0-9a-zぁ-んァ-ヶ一-龠ー]+", "", _normalized(name))
    if not compact_name:
        return False
    separator = r"[^0-9a-zぁ-んァ-ヶ一-龠ー]*"
    pattern = separator.join(re.escape(character) for character in compact_name)
    normalized_text = _normalized(text)
    for match in re.finditer(pattern, normalized_text):
        prefix = normalized_text[:match.start()]
        suffix = normalized_text[match.end():]
        left_matches = (
            not prefix
            or re.search(rf"[{_NAME_CHARACTERS}]$", prefix) is None
            or re.search(r"(?:店名は|店舗名は|お店は|店は|にある|の|昨日|今日|先日|さっき)$", prefix) is not None
        )
        right_matches = (
            not suffix
            or re.match(rf"[{_NAME_CHARACTERS}]", suffix) is None
            or (
                re.match(r"(?:はなれ|別館|本店|支店)", suffix) is None
                and re.match(r"(?:へ|で|を|に|は|が|の|と)", suffix) is not None
            )
        )
        if left_matches and right_matches:
            return True
    return False


def input_evidence_assessment(
    content: str,
    *,
    omitted_asset_count: int = 0,
    source_input_truncated: bool = False,
) -> EvidenceAssessment:
    reasons: list[str] = []
    if _TRUNCATION_MARKER.search(content):
        reasons.append("本文に続きの省略を示す表示があります。全文を確認してください。")
    if omitted_asset_count:
        reasons.append(f"添付等{omitted_asset_count}件が解析対象の上限を超えています。")
    if source_input_truncated:
        reasons.append("出典の本文または件数が検索入力の上限を超えています。")
    return EvidenceAssessment(
        codes=(EvidenceCode.INPUT_INCOMPLETE,) if reasons else (),
        reasons=tuple(reasons),
    )


def assess_mention_evidence(
    mention: MentionEvidence,
    content: str,
    *,
    input_assessment: EvidenceAssessment = EvidenceAssessment(),
    source_grounded: bool = False,
) -> EvidenceAssessment:
    codes = list(input_assessment.codes)
    reasons = list(input_assessment.reasons)
    non_store_kind = (
        mention.subject_kind if mention.subject_kind in {"product", "event"}
        else _explicit_non_store_subject(mention.shop_name, content)
    )
    if (
        non_store_kind == "event" and mention.subject_kind == "restaurant"
        and mention.identity_evidence == "explicit" and mention.name_evidence
        and _normalized(mention.name_evidence) in _normalized(content)
        and re.search(r"常設|実店舗|本店|店舗所在地|営業時間", mention.name_evidence)
        and _explicit_non_store_subject(mention.shop_name, mention.name_evidence) is None
    ):
        non_store_kind = None
    if non_store_kind is not None:
        codes.append(EvidenceCode.NON_STORE_SUBJECT)
        if non_store_kind == "event":
            codes.append(EvidenceCode.EVENT_EXCLUDED)
        reasons.append(_non_store_reason(non_store_kind))
    if mention.identity_evidence in {"ambiguous", "author_only", "unknown"} or mention.subject_kind == "unknown":
        codes.append(EvidenceCode.IDENTITY_UNCLEAR)
        reasons.append("店名の根拠が曖昧か投稿者名だけです。対象店舗を確認してください。")
    if mention.identity_evidence == "explicit" and not source_grounded:
        evidence = mention.name_evidence or ""
        non_author_content = "\n".join(
            line for line in content.splitlines()
            if not _AUTHOR_LINE.search(line.strip()) and not line.lstrip().startswith("[Attachment]")
        )
        if (
            not evidence
            or _normalized(evidence) not in _normalized(non_author_content)
            or not _contains_name(mention.shop_name, evidence)
        ):
            codes.append(EvidenceCode.NAME_UNGROUNDED)
            reasons.append("店名を裏付ける引用を投稿本文で確認できません。音写や推測で確定しません。")
        if mention.branch_name:
            branch_evidence = mention.branch_evidence or ""
            if (
                not branch_evidence
                or _normalized(branch_evidence) not in _normalized(non_author_content)
                or not _contains_name(mention.branch_name, branch_evidence)
            ):
                codes.append(EvidenceCode.BRANCH_UNGROUNDED)
                reasons.append("支店を裏付ける引用がありません。所在地だけから支店名を補いません。")
    return EvidenceAssessment(tuple(dict.fromkeys(codes)), tuple(dict.fromkeys(reasons)))


def event_exclusion_assessment(reason: str | None = None) -> EvidenceAssessment:
    return EvidenceAssessment(
        (EvidenceCode.NON_STORE_SUBJECT, EvidenceCode.EVENT_EXCLUDED),
        (_non_store_reason("event"), *((reason,) if reason else ())),
    )


def event_origin_quotes_match(
    *, name: str, branch: str | None, area: str | None,
    relation_evidence: str, permanent_evidence: str,
    relation_text: str, permanent_text: str,
) -> bool:
    if branch and re.search(r"催事|物産展|会場|期間限定|ポップアップ|pop.?up", branch, re.I):
        return False
    if not all(
        _normalized(quote) in _normalized("\n".join(
            line for line in text.splitlines()
            if not _AUTHOR_LINE.search(line.strip()) and not line.lstrip().startswith("[Attachment]")
        ))
        for quote, text in ((relation_evidence, relation_text), (permanent_evidence, permanent_text))
    ):
        return False
    full_name = f"{name} {branch}" if branch and branch not in name else name
    if not all(_contains_name(full_name, quote) for quote in (relation_evidence, permanent_evidence)):
        return False
    if area and _normalized(area) not in _normalized(permanent_evidence):
        return False
    if re.search(
        r"(?:常設|実店舗|本店|出店元).{0,16}(?:ありません|存在しない|なし|なく|持たず|不明)|"
        r"ではなく|ではない|ではありません|未確認|(?:出店).{0,8}(?:しない|しません)|"
        r"(?:旧店舗|移転前|閉鎖済みの会場)",
        f"{relation_evidence}\n{permanent_evidence}",
    ):
        return False
    name_pattern = r"\s*".join(re.escape(character) for character in _normalized(full_name).replace(" ", ""))
    direct_participation = re.search(
        rf"{name_pattern}[」』]?(?:が|から|より)[^。！\n]{{0,45}}出店", _normalized(relation_evidence),
    )
    return bool(
        (re.search(r"出店元|出店者|運営元", relation_evidence) or direct_participation)
        and re.search(r"常設|実店舗|本店|店舗所在地|営業時間|定休日", permanent_evidence)
    )


def evidence_review_error(assessment: EvidenceAssessment) -> str | None:
    if not assessment.requires_review:
        return None
    return _ERROR_PREFIX + json.dumps(
        {"codes": list(assessment.codes), "reasons": list(assessment.reasons)},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def requires_manual_evidence_review(error: str | None) -> bool:
    return bool(error and error.startswith(_ERROR_PREFIX))


def operating_status_note(mention: MentionEvidence) -> str | None:
    labels: dict[str, str] = {"open": "営業中", "closed": "閉店", "event_ended": "催事終了"}
    label = labels.get(mention.operating_status or "")
    if label is None:
        return None
    evidence = mention.operating_status_evidence or "根拠引用なし・要確認"
    return f"営業状態の記述（店舗の同定とは別）: {label} / 根拠: {evidence}"
