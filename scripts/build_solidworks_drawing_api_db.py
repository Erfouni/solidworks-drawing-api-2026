#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a searchable SQLite knowledge base for SOLIDWORKS drawing automation.

The crawler reads the public SOLIDWORKS 2026 English API Web Help, extracts
structured API facts, and stores compact normalized records rather than making
an offline verbatim mirror of the documentation.  Every record keeps its exact
official URL and crawl timestamp.

The script intentionally uses only the Python standard library plus lxml (which
is bundled with Codex desktop).  Network access is required only while crawling;
the resulting SQLite database is completely self contained.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import html as html_lib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from lxml import html as lxml_html


REPO = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO / "exports" / "solidworks_drawing_api_2026.sqlite"
DEFAULT_MANIFEST = REPO / "exports" / "solidworks_drawing_api_2026_manifest.json"
DEFAULT_README = REPO / "exports" / "solidworks_drawing_api_2026_README.md"

BASE_ORIGIN = "https://help.solidworks.com"
BASE_PREFIX = "/2026/english/api/"
SEED_URL = (
    BASE_ORIGIN
    + BASE_PREFIX
    + "sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.IDrawingDoc_members.html"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 SOLIDWORKS-drawing-API-indexer/1.0"
)


# Interfaces whose members directly participate in drawing creation,
# annotation, tables, sheet/view management, printing, and export.  Dependent
# common-document methods are added selectively from links and curated seeds.
DRAWING_INTERFACES = {
    "IDrawingDoc", "ISheet", "IView", "IDrawingComponent", "IPageSetup",
    "IPrintSpecification", "ILayerMgr", "ILayer", "IAnnotation", "INote",
    "IDisplayDimension", "IDimension", "IDimensionTolerance", "IGtol",
    "IGtolFrame", "ICenterMark", "ICenterLine", "IDetailCircle",
    "IProjectionArrow", "IDrSection", "IRevisionCloud", "IDatumTag",
    "IDatumTargetSym", "ISFSymbol", "IDowelSymbol", "IWeldSymbol",
    "IMultiJogLeader", "IBalloonStack", "IMagneticLine", "IBreakLine",
    "ITableAnnotation", "ITableAnchor", "IGeneralTableAnnotation",
    "IGeneralTableFeature", "IBomTableAnnotation", "IBomFeature",
    "IHoleTableAnnotation", "IHoleTable", "IRevisionTableAnnotation",
    "IRevisionTableFeature", "IBendTableAnnotation", "IBendTable",
    "IPunchTableAnnotation", "IPunchTable", "ITitleBlockTableAnnotation",
    "ITitleBlock", "IWeldmentCutListAnnotation", "IWeldmentCutListFeature",
    "ITextFormat", "IParagraphs", "IDisplayData", "ISilhouetteEdge",
    "ISketchHatch", "IAutoBalloonOptions", "IBalloonOptions",
    "IStackedBalloonOptions", "IExportPdfData",
}

COMMON_INTERFACES = {
    "ISldWorks", "IModelDoc2", "IModelDocExtension", "IModelDoc", "ISelectionMgr",
    "ISelectData", "IEntity", "IFeature", "IFeatureManager", "ISketchManager",
    "ISketch", "ISketchSegment", "ISketchPoint", "ISketchLine", "ISketchArc",
    "ISketchText", "IModelView", "IModelViewManager", "IModelWindow",
    "ICustomPropertyManager", "IConfiguration", "IConfigurationManager",
    "IComponent2", "IEdge", "IFace2", "IVertex", "ICurve", "IMathPoint",
    "IMathVector", "IMathTransform", "IMathUtility", "IUserUnit",
}

# Common-document members relevant to a drawing workflow.  Interface-specific
# drawing members are all included; these names gate broad common interfaces.
COMMON_MEMBER_ALLOW = {
    "ISldWorks": {
        "ActiveDoc", "ActivateDoc3", "CloseDoc", "GetActiveDoc", "GetDocumentCount",
        "GetDocuments", "GetExportFileData", "GetImportFileData", "GetMathUtility",
        "GetCurrentMacroPathFolder", "GetCurrentMacroPathName",
        "GetOpenDocumentByName", "GetUserPreferenceDoubleValue",
        "GetUserPreferenceIntegerValue", "GetUserPreferenceStringValue",
        "GetUserPreferenceToggle", "LoadFile4", "NewDocument", "OpenDoc6",
        "OpenDoc7", "RunCommand", "RunMacro2", "SetUserPreferenceDoubleValue",
        "SetUserPreferenceIntegerValue", "SetUserPreferenceStringValue",
        "SetUserPreferenceToggle", "Visible",
    },
    "IModelDoc2": {
        "AddDimension2", "AddHorizontalDimension2", "AddRadialDimension2",
        "AddVerticalDimension2", "ClearSelection2", "DeleteSelection2",
        "EditRebuild3", "Extension", "FeatureManager", "ForceRebuild3",
        "GetLayerManager", "GetPathName", "GetTitle", "GetType", "GetUserUnit", "GraphicsRedraw2", "InsertNote",
        "IsOpenedReadOnly", "ModelViewManager", "PrintDirect", "Save3",
        "SelectionManager", "SetAddToDB", "ShowConfiguration2", "SketchManager",
        "ViewZoomtofit2",
    },
    "IModelDocExtension": {
        "AddComment", "AddOrdinateDimension", "DeleteSelection2", "GetPersistReference3", "GetUserPreferenceDouble",
        "GetUserPreferenceInteger", "GetUserPreferenceString", "GetUserPreferenceToggle",
        "CustomPropertyManager", "CustomPropertyManager2", "InsertGeneralTableAnnotation", "InsertObjectFromFile", "PrintOut4",
        "RunCommand", "SaveAs2", "SaveAs3", "SelectByID2", "SetPersistReference3",
        "SetUserPreferenceDouble", "SetUserPreferenceInteger", "SetUserPreferenceString",
        "SetUserPreferenceToggle",
    },
    "ISelectionMgr": {
        "CreateSelectData", "DeSelect2", "GetSelectedObject6", "GetSelectedObjectCount2",
        "GetSelectedObjectType3", "GetSelectionPoint2", "GetSelectionSpecification",
        "SelectionColor", "SuspendSelectionList", "ResumeSelectionList",
    },
    "IEntity": {"DeSelect", "GetSafeEntity", "GetType", "Select4"},
    "ISketchManager": {
        "AddToDB", "Create3PointArc", "CreateArc", "CreateCenterLine", "CreateCircle",
        "CreateCircleByRadius", "CreateLine", "CreatePoint", "CreatePolygon",
        "CreateRectangle", "CreateSketchSlot", "CreateText", "InsertSketch",
    },
    "IFeatureManager": {"InsertDwgOrDxfFile2"},
    "IModelView": {"GraphicsRedraw", "Orientation3", "Scale2", "TranslateBy"},
    "IModelViewManager": {"LinkedViews", "ViewportDisplay"},
}

DRAWING_KEYWORDS = {
    "drawing", "sheet", "view", "annotation", "dimension", "balloon", "bom",
    "bill of materials", "table", "title block", "revision", "detail view",
    "section view", "projected view", "auxiliary view", "break view", "broken-out",
    "center mark", "centerline", "datum", "geometric tolerance", "gtol", "note",
    "leader", "weld symbol", "surface finish", "hole table", "layer", "print",
    "pdf", "dxf", "dwg", "slddrw", "drafting", "ordinate", "model item",
}

LANGUAGE_RE = re.compile(r"\b(VBA|VB\.NET|Visual Basic|C#|C\+\+|C\+\+/CLI|VB)\b", re.I)
API_TOKEN_RE = re.compile(
    r"\b(?:I[A-Z][A-Za-z0-9_]*::)?[A-Z][A-Za-z0-9_]*(?:\d+)?\b"
)


@dataclasses.dataclass
class Page:
    url: str
    title: str
    browser_title: str
    help_html: str
    toc: list[dict[str, Any]]
    next_prev: dict[str, Any]
    file_path: str
    fetched_at: str
    sha256: str
    http_bytes: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_url(url: str, base: str = BASE_ORIGIN) -> str:
    url = html_lib.unescape((url or "").strip())
    if not url:
        return ""
    absolute = urllib.parse.urljoin(base, url)
    parsed = urllib.parse.urlsplit(absolute)
    # Query ids are TOC state, not document identity.  Keep other query values
    # only if a page actually requires them (format/value are presentation only).
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if k.lower() not in {"id", "format", "value"}]
    path = re.sub(r"/{2,}", "/", parsed.path)
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, urllib.parse.urlencode(query), ""))


def is_official_api_url(url: str) -> bool:
    p = urllib.parse.urlsplit(normalize_url(url))
    return p.netloc == "help.solidworks.com" and p.path.lower().startswith(BASE_PREFIX)


def fetch_page(url: str, retries: int = 4, timeout: int = 60) -> Page:
    url = normalize_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read()
            text = raw.decode("utf-8", errors="replace")
            m = re.search(
                r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
                text,
                flags=re.S | re.I,
            )
            if not m:
                raise ValueError("__NEXT_DATA__ not found")
            data = json.loads(m.group(1))
            props = data["props"]["pageProps"]
            content = props.get("helpContentData") or {}
            help_html = content.get("helpText") or ""
            if not isinstance(help_html, str):
                help_html = json.dumps(help_html, ensure_ascii=False)
            return Page(
                url=url,
                title=clean_text(content.get("title") or ""),
                browser_title=clean_text(content.get("browserTitle") or ""),
                help_html=help_html,
                toc=props.get("allGuidesSectionData") or [],
                next_prev=props.get("nextPrevData") or {},
                file_path=props.get("filePath") or "",
                fetched_at=utc_now(),
                sha256=hashlib.sha256(raw).hexdigest(),
                http_bytes=len(raw),
            )
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last = exc
            if attempt + 1 < retries:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"Failed to fetch {url}: {last}")


def parse_fragment(fragment: str):
    fragment = fragment or ""
    try:
        return lxml_html.fragment_fromstring(fragment, create_parent="div")
    except Exception:
        return lxml_html.fragment_fromstring("<div></div>", create_parent="div")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    value = html_lib.unescape(value)
    value = value.replace("\xa0", " ")
    value = re.sub(r"[ \t\f\v]+", " ", value)
    value = re.sub(r"\s*\n\s*", "\n", value)
    return value.strip()


def node_text(node) -> str:
    if node is None or not isinstance(getattr(node, "tag", None), str):
        return ""
    return clean_text(" ".join(node.itertext()))


def iter_toc(nodes: Iterable[dict[str, Any]], trail: tuple[str, ...] = ()) -> Iterator[tuple[dict[str, Any], tuple[str, ...]]]:
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        name = clean_text(node.get("name") or "")
        current = trail + ((name,) if name else ())
        yield node, current
        for key, value in node.items():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                yield from iter_toc(value, current)


def html_links(page: Page) -> list[tuple[str, str]]:
    root = parse_fragment(page.help_html)
    out: list[tuple[str, str]] = []
    seen = set()
    for a in root.xpath(".//a[@href]"):
        url = normalize_url(a.get("href"), page.url)
        text = node_text(a)
        if url and (url, text) not in seen:
            seen.add((url, text))
            out.append((url, text))
    return out


def heading_sections(page: Page) -> list[tuple[str, str]]:
    """Return compact heading/body sections from the help fragment."""
    root = parse_fragment(page.help_html)
    blocks: list[tuple[str, list[str]]] = []
    current_title = "Overview"
    current: list[str] = []
    for elem in root.iter():
        tag = str(elem.tag).lower() if isinstance(elem.tag, str) else ""
        if tag in {"h1", "h2", "h3", "h4", "h5"}:
            text = node_text(elem)
            if current:
                blocks.append((current_title, current))
            current_title, current = text or "Section", []
        elif tag in {"p", "pre", "table", "ul", "ol", "dl"}:
            text = node_text(elem)
            if text and (not current or current[-1] != text):
                current.append(text)
    if current:
        blocks.append((current_title, current))
    return [(title, clean_text("\n".join(lines))) for title, lines in blocks]


def section_map(page: Page) -> dict[str, str]:
    result: dict[str, str] = {}
    for title, body in heading_sections(page):
        key = title.casefold()
        result[key] = clean_text((result.get(key, "") + "\n" + body).strip())
    return result


def compact_summary(text: str, max_chars: int = 1200) -> str:
    text = clean_text(text)
    text = re.sub(r"(?i)\b(SOLIDWORKS API Help|Learning Resources|See Also)\b", "", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    for marker in (". ", "; ", "\n"):
        idx = cut.rfind(marker, max_chars // 2)
        if idx > 0:
            return cut[: idx + len(marker)].strip()
    return cut.rstrip() + "…"


def inspect(url: str) -> int:
    page = fetch_page(url)
    print(json.dumps({
        "url": page.url,
        "title": page.title,
        "browser_title": page.browser_title,
        "file_path": page.file_path,
        "http_bytes": page.http_bytes,
        "help_html_chars": len(page.help_html),
        "toc_roots": len(page.toc),
        "toc_root_keys": [sorted(x.keys()) for x in page.toc[:3]],
        "toc_first_nodes": [
            {"name": n.get("name"), "url": n.get("url"), "trail": list(trail), "keys": sorted(n.keys())}
            for n, trail in list(iter_toc(page.toc))[:20]
        ],
        "links_count": len(html_links(page)),
        "links_first": html_links(page)[:50],
        "sections": [(h, len(b), b[:500]) for h, b in heading_sections(page)[:20]],
    }, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Local official-help ingestion
# ---------------------------------------------------------------------------

LOCAL_HELP = {
    "sldworksapi": REPO / "work" / "sa",
    "sldworksapivb6": REPO / "work" / "svb",
    "swconst": REPO / "work" / "sc",
    "sldworksapiprogguide": REPO / "work" / "spg",
    "swcommands": REPO / "work" / "scmd",
    "swdocmgrapi": REPO / "work" / "sdm",
}

SOURCE_CHMS = {
    "sldworksapi": Path(r"C:\Program Files\SW\SOLIDWORKS\api\sldworksapi.chm"),
    "sldworksapivb6": Path(r"C:\Program Files\SW\SOLIDWORKS\api\sldworksapivb6.chm"),
    "swconst": Path(r"C:\Program Files\SW\SOLIDWORKS\api\swconst.chm"),
    "sldworksapiprogguide": Path(r"C:\Program Files\SW\SOLIDWORKS\api\sldworksapiprogguide.chm"),
    "swcommands": Path(r"C:\Program Files\SW\SOLIDWORKS\api\swcommands.chm"),
    "swdocmgrapi": Path(r"C:\Program Files\SW\SOLIDWORKS\api\swdocmgrapi.chm"),
}

SOURCE_TITLES = {
    "sldworksapi": "SOLIDWORKS API Help 2026",
    "sldworksapivb6": "SOLIDWORKS VBA Type Library Help 2026",
    "swconst": "SOLIDWORKS API Enumerations 2026",
    "sldworksapiprogguide": "SOLIDWORKS API Programming Guide 2026",
    "swcommands": "SOLIDWORKS Command IDs 2026",
    "swdocmgrapi": "SOLIDWORKS Document Manager API Help 2026",
}

SOURCE_NOTES = {
    "sldworksapi": "Primary .NET/C++ API type, member, event, and example reference.",
    "sldworksapivb6": "VBA signatures linked from the primary member pages.",
    "swconst": "Enumerations and numeric values used by drawing API parameters and results.",
    "sldworksapiprogguide": "Official programming, macro, COM, add-in, and object-model guidance.",
    "swcommands": "Command identifiers used with RunCommand.",
    "swdocmgrapi": "Read-only/offline document, sheet, view, preview, and reference inspection API.",
}

SCHEMA_SQL = r"""
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=DELETE;
PRAGMA synchronous=FULL;

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE source_sets (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    product_version TEXT NOT NULL,
    service_pack TEXT,
    source_kind TEXT NOT NULL,
    installed_chm_path TEXT,
    chm_sha256 TEXT,
    chm_bytes INTEGER,
    installed_modified_at TEXT,
    extracted_root TEXT,
    extracted_files INTEGER NOT NULL DEFAULT 0,
    selected_pages INTEGER NOT NULL DEFAULT 0,
    official_root_url TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE source_pages (
    id INTEGER PRIMARY KEY,
    source_set_id INTEGER NOT NULL REFERENCES source_sets(id),
    relative_path TEXT NOT NULL,
    title TEXT NOT NULL,
    topic_kind TEXT NOT NULL,
    official_url TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    file_bytes INTEGER NOT NULL,
    extracted_at TEXT NOT NULL,
    UNIQUE(source_set_id, relative_path)
);

CREATE TABLE api_types (
    id INTEGER PRIMARY KEY,
    source_page_id INTEGER NOT NULL UNIQUE REFERENCES source_pages(id),
    library TEXT NOT NULL,
    namespace TEXT NOT NULL,
    name TEXT NOT NULL,
    display_name TEXT,
    type_kind TEXT NOT NULL,
    summary TEXT,
    remarks TEXT,
    syntax_vbnet TEXT,
    syntax_csharp TEXT,
    syntax_cppcli TEXT,
    syntax_vba TEXT,
    availability TEXT,
    access_path TEXT,
    member_count INTEGER NOT NULL DEFAULT 0,
    search_terms TEXT,
    official_url TEXT NOT NULL,
    UNIQUE(library, name)
);

CREATE TABLE api_members (
    id INTEGER PRIMARY KEY,
    source_page_id INTEGER NOT NULL UNIQUE REFERENCES source_pages(id),
    type_id INTEGER NOT NULL REFERENCES api_types(id),
    name TEXT NOT NULL,
    member_kind TEXT NOT NULL,
    summary TEXT,
    signature_vba TEXT,
    signature_vbnet TEXT,
    signature_vbnet_usage TEXT,
    signature_csharp TEXT,
    signature_cppcli TEXT,
    return_description TEXT,
    remarks TEXT,
    availability TEXT,
    is_obsolete INTEGER NOT NULL DEFAULT 0,
    superseded_by TEXT,
    selection_requirements TEXT,
    unit_notes TEXT,
    drawing_tasks TEXT,
    enum_references TEXT,
    example_count INTEGER NOT NULL DEFAULT 0,
    official_url TEXT NOT NULL,
    UNIQUE(type_id, name)
);

CREATE TABLE api_parameters (
    id INTEGER PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES api_members(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    type_vba TEXT,
    type_dotnet TEXT,
    direction TEXT,
    description TEXT,
    unit TEXT,
    enum_name TEXT,
    UNIQUE(member_id, position)
);

CREATE TABLE api_enums (
    id INTEGER PRIMARY KEY,
    source_page_id INTEGER NOT NULL UNIQUE REFERENCES source_pages(id),
    library TEXT NOT NULL,
    name TEXT NOT NULL,
    summary TEXT,
    remarks TEXT,
    syntax_vbnet TEXT,
    syntax_csharp TEXT,
    availability TEXT,
    referenced_by_count INTEGER NOT NULL DEFAULT 0,
    drawing_relevance TEXT,
    official_url TEXT NOT NULL,
    UNIQUE(library, name)
);

CREATE TABLE api_enum_values (
    id INTEGER PRIMARY KEY,
    enum_id INTEGER NOT NULL REFERENCES api_enums(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    numeric_value TEXT,
    description TEXT,
    UNIQUE(enum_id, name)
);

CREATE TABLE examples (
    id INTEGER PRIMARY KEY,
    source_page_id INTEGER NOT NULL UNIQUE REFERENCES source_pages(id),
    title TEXT NOT NULL,
    language TEXT,
    objective TEXT,
    prerequisites TEXT,
    postconditions TEXT,
    api_types_json TEXT NOT NULL,
    api_calls_json TEXT NOT NULL,
    destructive_side_effects TEXT,
    relevance_reason TEXT,
    search_terms TEXT,
    official_url TEXT NOT NULL
);

CREATE TABLE example_calls (
    id INTEGER PRIMARY KEY,
    example_id INTEGER NOT NULL REFERENCES examples(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    api_type TEXT,
    api_member TEXT NOT NULL,
    resolved_member_id INTEGER REFERENCES api_members(id),
    UNIQUE(example_id, sequence)
);

CREATE TABLE guide_topics (
    id INTEGER PRIMARY KEY,
    source_page_id INTEGER NOT NULL UNIQUE REFERENCES source_pages(id),
    title TEXT NOT NULL,
    category TEXT,
    summary TEXT,
    section_outline TEXT,
    search_terms TEXT,
    official_url TEXT NOT NULL
);

CREATE TABLE workflows (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    goal TEXT NOT NULL,
    recommended_when TEXT,
    avoid_when TEXT,
    prerequisites TEXT,
    verification TEXT,
    pitfalls TEXT,
    primary_api_chain TEXT,
    search_terms TEXT,
    source_urls_json TEXT NOT NULL
);

CREATE TABLE workflow_steps (
    id INTEGER PRIMARY KEY,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    instruction TEXT NOT NULL,
    api_symbols TEXT,
    expected_result TEXT,
    UNIQUE(workflow_id, sequence)
);

CREATE TABLE relationships (
    id INTEGER PRIMARY KEY,
    from_kind TEXT NOT NULL,
    from_id INTEGER NOT NULL,
    relation TEXT NOT NULL,
    to_kind TEXT NOT NULL,
    to_id INTEGER,
    to_symbol TEXT,
    evidence_url TEXT,
    UNIQUE(from_kind, from_id, relation, to_kind, to_symbol)
);

CREATE TABLE aliases (
    id INTEGER PRIMARY KEY,
    canonical TEXT NOT NULL,
    alias TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'en',
    category TEXT,
    UNIQUE(canonical, alias, language)
);

CREATE TABLE coverage (
    id INTEGER PRIMARY KEY,
    area TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    records INTEGER NOT NULL,
    scope_rule TEXT NOT NULL,
    limitations TEXT,
    checked_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE search_fts USING fts5(
    entity_kind UNINDEXED,
    entity_id UNINDEXED,
    title,
    body,
    tags,
    source_url UNINDEXED,
    tokenize='unicode61 remove_diacritics 2 tokenchars ''_:'''
);

CREATE INDEX idx_pages_kind ON source_pages(topic_kind);
CREATE INDEX idx_types_name ON api_types(name);
CREATE INDEX idx_members_name ON api_members(name);
CREATE INDEX idx_members_type ON api_members(type_id);
CREATE INDEX idx_params_member ON api_parameters(member_id);
CREATE INDEX idx_enum_values_name ON api_enum_values(name);
CREATE INDEX idx_examples_language ON examples(language);
CREATE INDEX idx_calls_member ON example_calls(api_member);
CREATE INDEX idx_workflows_category ON workflows(category);

CREATE VIEW v_api_member_details AS
SELECT
    t.library,
    t.name AS api_type,
    m.name AS api_member,
    m.member_kind,
    m.summary,
    m.signature_vba,
    m.signature_csharp,
    m.return_description,
    m.remarks,
    m.availability,
    m.is_obsolete,
    m.superseded_by,
    m.selection_requirements,
    m.unit_notes,
    m.drawing_tasks,
    m.enum_references,
    m.example_count,
    m.official_url
FROM api_members m
JOIN api_types t ON t.id=m.type_id;

CREATE VIEW v_workflow_api_map AS
SELECT w.slug, w.title, w.category, s.sequence, s.instruction, s.api_symbols,
       s.expected_result, w.verification, w.pitfalls, w.source_urls_json
FROM workflows w JOIN workflow_steps s ON s.workflow_id=w.id
ORDER BY w.slug, s.sequence;
"""


MAIN_FILE_RE = re.compile(
    r"^SolidWorks\.Interop\.sldworks~SolidWorks\.Interop\.sldworks\.([^~]+?)(?:~([^.]+))?\.html$",
    re.I,
)
DM_FILE_RE = re.compile(
    r"^SolidWorks\.Interop\.swdocumentmgr~SolidWorks\.Interop\.swdocumentmgr\.([^~]+?)(?:~([^.]+))?\.html$",
    re.I,
)

TYPE_LIST_SUFFIXES = ("_members", "_methods", "_properties", "_events")
DOCMGR_DIRECT_TYPES = {
    "ISwDMSheet", "ISwDMSheet2", "ISwDMSheet3", "ISwDMSheet4",
    "ISwDMView", "ISwDMView2", "ISwDMTable", "ISwDMTable2", "ISwDMTable3",
    "ISwDMTable4", "ISwDMTable5",
}
DOCMGR_COMMON_TYPES = {
    "ISwDMApplication", "ISwDMApplication2", "ISwDMApplication3", "ISwDMApplication4",
    "ISwDMApplication5", "ISwDMDocument", "ISwDMDocument3",
    "ISwDMDocument4", "ISwDMDocument5", "ISwDMDocument6", "ISwDMDocument7",
    "ISwDMDocument8", "ISwDMDocument10", "ISwDMDocument11",
    "ISwDMDocument12", "ISwDMDocument13", "ISwDMDocument14", "ISwDMDocument15",
    "ISwDMDocument16", "ISwDMDocument17", "ISwDMDocument18", "ISwDMDocument19",
    "ISwDMDocument20", "ISwDMDocument21", "ISwDMDocument22", "ISwDMDocument23",
    "ISwDMDocument24", "ISwDMDocument25", "ISwDMDocument26", "ISwDMDocument27",
    "ISwDMDocument28", "ISwDMDocument29", "ISwDMDocument30", "ISwDMDocument31",
}
DM_MEMBER_RE = re.compile(
    r"(?i)(drawing|sheet|view|preview|reference|document|open|close|save|property|table|configuration|stream)"
)

STOP_CALLS = {
    "As", "ByRef", "ByVal", "Call", "Case", "Dim", "Do", "Each", "Else", "End",
    "False", "For", "Function", "If", "In", "Is", "Loop", "Next", "Nothing", "Null",
    "Option", "Public", "Set", "Sub", "Then", "True", "While", "Wend", "With",
    "Debug", "Print", "String", "Double", "Integer", "Long", "Boolean", "Variant",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_local_html(path: Path):
    raw = path.read_bytes()
    if not raw.strip():
        return lxml_html.fromstring("<html><body></body></html>"), raw
    try:
        root = lxml_html.fromstring(raw)
    except Exception:
        root = lxml_html.fromstring(raw.decode("windows-1252", errors="replace"))
    return root, raw


def xpath_text(root, expression: str) -> str:
    nodes = root.xpath(expression)
    if not nodes:
        return ""
    node = nodes[0]
    return node_text(node) if hasattr(node, "itertext") else clean_text(node)


def page_title(root, fallback: str = "") -> str:
    return (
        xpath_text(root, "//*[@id='pagetitle'][1]")
        or xpath_text(root, "//title[1]")
        or xpath_text(root, "//h1[last()]")
        or fallback
    )


def section_text_local(root, section_id: str, max_chars: int = 2400) -> str:
    text = xpath_text(root, f"//*[@id='{section_id}'][1]")
    return compact_summary(text, max_chars=max_chars)


def leading_summary_local(root, max_chars: int = 1000) -> str:
    mains = root.xpath("//*[@id='mainbody'][1]")
    if not mains:
        body = xpath_text(root, "//body[1]")
        return compact_summary(body, max_chars=max_chars)
    main = mains[0]
    parts: list[str] = []
    if main.text:
        parts.append(main.text)
    for child in main:
        tag = str(child.tag).lower() if isinstance(child.tag, str) else ""
        if tag in {"h1", "h2"} or child.get("id") in {"syntaxSection", "exampleSection"}:
            break
        if child.get("id") != "allHistory":
            parts.append(node_text(child))
        if child.tail:
            parts.append(child.tail)
    return compact_summary(" ".join(parts), max_chars=max_chars)


def syntax_blocks(root) -> dict[str, str]:
    out = {}
    for key, sid in (
        ("vbnet", "Syntax_VB"),
        ("vbnet_usage", "Syntax_VBUsage"),
        ("csharp", "Syntax_CS"),
        ("cppcli", "Syntax_CPP2005"),
    ):
        nodes = root.xpath(f"//*[@id='{sid}'][1]//pre[1]")
        out[key] = node_text(nodes[0]) if nodes else ""
    return out


def extract_parameters(root, signatures: dict[str, str]) -> list[dict[str, str]]:
    mains = root.xpath("//*[@id='mainbody'][1]")
    if not mains:
        return []
    dls = mains[0].xpath(".//h4[translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='parameters']/following-sibling::dl[1]")
    if not dls:
        return []
    dl = dls[0]
    result = []
    dts = dl.xpath("./dt")
    for pos, dt in enumerate(dts, 1):
        name = node_text(dt)
        dd = dt.getnext()
        description = node_text(dd) if dd is not None and str(dd.tag).lower() == "dd" else ""
        vb_sig = signatures.get("vbnet", "")
        cs_sig = signatures.get("csharp", "")
        vba_type = ""
        dotnet_type = ""
        direction = ""
        m = re.search(rf"(?i)\b(ByVal|ByRef)\s+{re.escape(name)}\s+As\s+([\w.]+)", vb_sig)
        if m:
            direction = m.group(1)
            dotnet_type = m.group(2)
        m2 = re.search(rf"(?i)(?:\b(ref|out|in)\s+)?([\w.^]+)\s+{re.escape(name)}\b", cs_sig)
        if m2:
            direction = direction or (m2.group(1) or "ByVal")
            dotnet_type = dotnet_type or m2.group(2)
        unit = ""
        low = description.casefold()
        for token in ("meters", "meter", "millimeters", "millimeter", "radians", "radian", "degrees", "degree"):
            if token in low:
                unit = token
                break
        enum_match = re.search(r"\b(sw[A-Za-z0-9_]+_e)\b", description)
        result.append({
            "position": pos,
            "name": name,
            "type_vba": vba_type,
            "type_dotnet": dotnet_type,
            "direction": direction,
            "description": compact_summary(description, 900),
            "unit": unit,
            "enum_name": enum_match.group(1) if enum_match else "",
        })
    return result


def extract_return_value(root) -> str:
    mains = root.xpath("//*[@id='mainbody'][1]")
    if not mains:
        return ""
    hs = mains[0].xpath(".//h4[translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='return value']")
    if not hs:
        return ""
    h = hs[0]
    parts = [h.tail or ""]
    node = h.getnext()
    while node is not None:
        tag = str(node.tag).lower() if isinstance(node.tag, str) else ""
        if tag in {"h1", "h2", "h3", "h4"}:
            break
        parts.append(node_text(node))
        parts.append(node.tail or "")
        node = node.getnext()
    return compact_summary(" ".join(parts), 900)


def sentences_containing(text: str, needles: Iterable[str], max_chars: int = 1600) -> str:
    found = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", clean_text(text)):
        low = sentence.casefold()
        if any(n.casefold() in low for n in needles) and sentence not in found:
            found.append(sentence)
    return compact_summary(" ".join(found), max_chars=max_chars)


def extract_obsolete(summary: str, remarks: str) -> tuple[int, str]:
    # Current member remarks often mention *other*, now-obsolete overloads.
    # Only the official leading summary marks the current page obsolete.
    blob = clean_text(summary)
    obsolete = int(bool(re.match(r"(?i)^Obsolete\b", blob)))
    m = re.search(r"(?i)Superseded by\s+([^.;\n]+)", blob) if obsolete else None
    return obsolete, clean_text(m.group(1)) if m else ""


def task_tags(name: str, summary: str, remarks: str) -> str:
    blob = (name + " " + summary + " " + remarks).casefold()
    mapping = {
        "document-lifecycle": ("newdocument", "opendoc", "closedoc", "save", "drawing document"),
        "sheets": ("sheet", "title block", "sheet format"),
        "views": ("drawing view", "projected view", "section view", "detail view", "auxiliary view", "view position"),
        "dimensions": ("dimension", "ordinate", "hole callout"),
        "annotations": ("annotation", "note", "balloon", "datum", "gtol", "weld symbol", "center mark", "centerline"),
        "tables-bom": ("table", "bill of materials", "bom", "revision table"),
        "layers-style": ("layer", "line style", "line font"),
        "selection": ("select", "selection"),
        "export-print": ("pdf", "dxf", "dwg", "export", "print"),
        "traversal": ("getfirst", "getnext", "count", "list"),
    }
    return ",".join(k for k, values in mapping.items() if any(v in blob for v in values))


def member_kind_from_title(title: str) -> str:
    low = title.casefold()
    for kind in ("method", "property", "event", "delegate", "field"):
        if kind in low:
            return kind
    return "member"


def type_kind_from_title(title: str, name: str) -> str:
    low = title.casefold()
    if "delegate" in low or "eventhandler" in name.casefold():
        return "event_delegate"
    for kind in ("interface", "class", "structure", "enumeration", "delegate"):
        if kind in low:
            return kind
    return "interface" if name.startswith("I") else "type"


def page_url(source_code: str, relative_path: str) -> str:
    rel = relative_path.replace("\\", "/")
    return f"{BASE_ORIGIN}/2026/English/api/{source_code}/{urllib.parse.quote(rel, safe='/~._-()')}"


def insert_source_page(
    conn: sqlite3.Connection,
    source_set_id: int,
    source_code: str,
    path: Path,
    title: str,
    topic_kind: str,
    raw: bytes,
) -> tuple[int, str]:
    root = LOCAL_HELP[source_code]
    rel = path.relative_to(root).as_posix()
    url = page_url(source_code, rel)
    cur = conn.execute(
        "INSERT INTO source_pages(source_set_id,relative_path,title,topic_kind,official_url,content_sha256,file_bytes,extracted_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (source_set_id, rel, title, topic_kind, url, hashlib.sha256(raw).hexdigest(), len(raw), utc_now()),
    )
    return int(cur.lastrowid), url


def create_source_sets(conn: sqlite3.Connection) -> dict[str, int]:
    ids = {}
    for code, root in LOCAL_HELP.items():
        if not root.exists():
            raise FileNotFoundError(f"Extracted official help is missing: {root}")
        chm = SOURCE_CHMS[code]
        stat = chm.stat() if chm.exists() else None
        count = sum(1 for p in root.rglob("*") if p.is_file())
        cur = conn.execute(
            "INSERT INTO source_sets(code,title,product_version,service_pack,source_kind,installed_chm_path,chm_sha256,chm_bytes,"
            "installed_modified_at,extracted_root,extracted_files,official_root_url,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                code,
                SOURCE_TITLES[code],
                "2026",
                "installed build dated 2026-04-11",
                "official installed CHM + mapped Web Help URLs",
                f"SOLIDWORKS 2026/api/{chm.name}",
                sha256_file(chm) if chm.exists() else "",
                stat.st_size if stat else 0,
                datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat() if stat else "",
                f"work/{root.name}",
                count,
                f"{BASE_ORIGIN}/2026/English/api/{code}/",
                SOURCE_NOTES[code],
            ),
        )
        ids[code] = int(cur.lastrowid)
    return ids


def selected_main_type(name: str) -> bool:
    return (
        name in DRAWING_INTERFACES
        or name in COMMON_INTERFACES
        or name.startswith("DDrawingDocEvents_")
    )


def selected_main_member(type_name: str, member_name: str) -> bool:
    if type_name in DRAWING_INTERFACES:
        return True
    if type_name in COMMON_INTERFACES:
        allowed = COMMON_MEMBER_ALLOW.get(type_name)
        return bool(allowed and member_name in allowed)
    return False


def selected_dm_type(name: str) -> bool:
    return name in DOCMGR_DIRECT_TYPES or name in DOCMGR_COMMON_TYPES


def selected_dm_member(type_name: str, member_name: str) -> bool:
    if type_name in DOCMGR_DIRECT_TYPES:
        return True
    return bool(DM_MEMBER_RE.search(member_name))


def find_vba_signature(type_name: str, member_name: Optional[str] = None) -> str:
    base = type_name[1:] if type_name.startswith("I") else type_name
    candidates = []
    if member_name:
        candidates += [f"SldWorks~{base}~{member_name}.html", f"sldworks~{base}~{member_name}.html"]
    else:
        candidates += [f"SldWorks~{base}.html", f"sldworks~{base}.html"]
    root_dir = LOCAL_HELP["sldworksapivb6"]
    for filename in candidates:
        path = root_dir / filename
        if path.exists():
            root, _ = read_local_html(path)
            nodes = root.xpath("//*[@id='Syntax_VB'][1]//pre[1] | //*[@id='syntaxSection'][1]//pre[1]")
            if nodes:
                return node_text(nodes[0])
    return ""


def extract_enum_refs(root, summary: str, remarks: str, params: list[dict[str, str]]) -> list[str]:
    refs = set(re.findall(r"\b(sw[A-Za-z0-9_]+_e)\b", summary + " " + remarks))
    for p in params:
        if p.get("enum_name"):
            refs.add(p["enum_name"])
    for a in root.xpath("//a[@href]"):
        href = a.get("href") or ""
        text = node_text(a)
        if "swconst" in href.casefold() or re.match(r"^sw[A-Za-z0-9_]+_e$", text):
            m = re.search(r"\b(sw[A-Za-z0-9_]+_e)\b", text + " " + href)
            if m:
                refs.add(m.group(1))
    return sorted(refs)


def linked_examples(root) -> list[str]:
    out = []
    for a in root.xpath("//*[@id='exampleSection'][1]//a[@href]"):
        href = urllib.parse.unquote((a.get("href") or "").split("#", 1)[0])
        if href.lower().endswith((".htm", ".html")):
            out.append(Path(href.replace("\\", "/")).name.casefold())
    return out


def ingest_api_library(
    conn: sqlite3.Connection,
    source_ids: dict[str, int],
    source_code: str,
) -> dict[str, Any]:
    root_dir = LOCAL_HELP[source_code]
    regex = MAIN_FILE_RE if source_code == "sldworksapi" else DM_FILE_RE
    namespace = "SolidWorks.Interop.sldworks" if source_code == "sldworksapi" else "SolidWorks.Interop.swdocumentmgr"
    library = "sldworks" if source_code == "sldworksapi" else "swdocumentmgr"
    is_type_selected = selected_main_type if source_code == "sldworksapi" else selected_dm_type
    is_member_selected = selected_main_member if source_code == "sldworksapi" else selected_dm_member

    parsed_files = []
    type_pages: dict[str, Path] = {}
    member_pages: list[tuple[str, str, Path]] = []
    for path in root_dir.glob("*.html"):
        if path.stat().st_size == 0:
            continue
        m = regex.match(path.name)
        if not m:
            continue
        type_name, member_name = m.group(1), m.group(2)
        if any(type_name.endswith(s) for s in TYPE_LIST_SUFFIXES):
            continue
        if member_name is None:
            if is_type_selected(type_name):
                type_pages[type_name] = path
        elif is_member_selected(type_name, member_name):
            member_pages.append((type_name, member_name, path))

    # A few selected types can lack a standalone page in an old library slice;
    # create no synthetic facts, but report them in coverage later.
    type_ids: dict[str, int] = {}
    for type_name, path in sorted(type_pages.items()):
        root, raw = read_local_html(path)
        title = page_title(root, type_name)
        summary = leading_summary_local(root)
        remarks = section_text_local(root, "remarksSection")
        syntax = syntax_blocks(root)
        availability = section_text_local(root, "availabilitySection", 600)
        access = section_text_local(root, "swobjectmodelSection", 900)
        vba = find_vba_signature(type_name) if source_code == "sldworksapi" else ""
        page_id, url = insert_source_page(
            conn, source_ids[source_code], source_code, path, title, "api_type", raw
        )
        cur = conn.execute(
            "INSERT INTO api_types(source_page_id,library,namespace,name,display_name,type_kind,summary,remarks,syntax_vbnet,"
            "syntax_csharp,syntax_cppcli,syntax_vba,availability,access_path,search_terms,official_url) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                page_id, library, namespace, type_name, type_name[1:] if type_name.startswith("I") else type_name,
                type_kind_from_title(title, type_name), summary, remarks, syntax.get("vbnet", ""), syntax.get("csharp", ""),
                syntax.get("cppcli", ""), vba, availability, access,
                task_tags(type_name, summary, remarks), url,
            ),
        )
        type_ids[type_name] = int(cur.lastrowid)
        parsed_files.append(path.name)

    enum_refs: Counter[str] = Counter()
    example_links: set[str] = set()
    relationship_buffer: list[tuple[str, int, str, str, Optional[int], str, str]] = []
    for type_name, member_name, path in sorted(member_pages):
        type_id = type_ids.get(type_name)
        if not type_id:
            continue
        root, raw = read_local_html(path)
        title = page_title(root, f"{member_name} ({type_name})")
        summary = leading_summary_local(root)
        remarks = section_text_local(root, "remarksSection", 2800)
        syntax = syntax_blocks(root)
        params = extract_parameters(root, syntax)
        enum_names = extract_enum_refs(root, summary, remarks, params)
        for enum_name in enum_names:
            enum_refs[enum_name] += 1
        links = linked_examples(root)
        example_links.update(links)
        obsolete, superseded = extract_obsolete(summary, remarks)
        selection = sentences_containing(summary + " " + remarks, ("select", "selection"), 1500)
        units = sentences_containing(
            summary + " " + remarks + " " + " ".join(p["description"] for p in params),
            ("meter", "radian", "degree", "millimeter", "unit"),
            1200,
        )
        availability = section_text_local(root, "availabilitySection", 600)
        returns = extract_return_value(root)
        vba = find_vba_signature(type_name, member_name) if source_code == "sldworksapi" else ""
        page_id, url = insert_source_page(
            conn, source_ids[source_code], source_code, path, title, "api_member", raw
        )
        cur = conn.execute(
            "INSERT INTO api_members(source_page_id,type_id,name,member_kind,summary,signature_vba,signature_vbnet,"
            "signature_vbnet_usage,signature_csharp,signature_cppcli,return_description,remarks,availability,is_obsolete,"
            "superseded_by,selection_requirements,unit_notes,drawing_tasks,enum_references,example_count,official_url) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                page_id, type_id, member_name, member_kind_from_title(title), summary, vba,
                syntax.get("vbnet", ""), syntax.get("vbnet_usage", ""), syntax.get("csharp", ""),
                syntax.get("cppcli", ""), returns, remarks, availability, obsolete, superseded,
                selection, units, task_tags(member_name, summary, remarks), ",".join(enum_names), len(links), url,
            ),
        )
        member_id = int(cur.lastrowid)
        for p in params:
            # VBA type is filled from the VBA signature when available.
            if vba:
                m = re.search(rf"(?i)\b(ByVal|ByRef)\s+{re.escape(p['name'])}\s+As\s+([\w.]+)", vba)
                if m:
                    p["type_vba"] = m.group(2)
                    p["direction"] = p["direction"] or m.group(1)
            conn.execute(
                "INSERT INTO api_parameters(member_id,position,name,type_vba,type_dotnet,direction,description,unit,enum_name) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    member_id, p["position"], p["name"], p["type_vba"], p["type_dotnet"], p["direction"],
                    p["description"], p["unit"], p["enum_name"],
                ),
            )
        for enum_name in enum_names:
            relationship_buffer.append(("api_member", member_id, "uses_enum", "api_enum", None, enum_name, url))
        if superseded:
            relationship_buffer.append(("api_member", member_id, "superseded_by", "api_member", None, superseded, url))
        parsed_files.append(path.name)

    for type_id in type_ids.values():
        count = conn.execute("SELECT COUNT(*) FROM api_members WHERE type_id=?", (type_id,)).fetchone()[0]
        conn.execute("UPDATE api_types SET member_count=? WHERE id=?", (count, type_id))

    for row in relationship_buffer:
        conn.execute(
            "INSERT OR IGNORE INTO relationships(from_kind,from_id,relation,to_kind,to_id,to_symbol,evidence_url) "
            "VALUES(?,?,?,?,?,?,?)",
            row,
        )
    return {
        "type_ids": type_ids,
        "enum_refs": enum_refs,
        "example_links": example_links,
        "selected_files": parsed_files,
        "candidate_types": sorted(type_pages),
        "missing_declared_types": (
            sorted((DRAWING_INTERFACES | COMMON_INTERFACES) - set(type_pages))
            if source_code == "sldworksapi"
            else sorted((DOCMGR_DIRECT_TYPES | DOCMGR_COMMON_TYPES) - set(type_pages))
        ),
    }


def enum_is_relevant(name: str, summary: str, values_text: str, referenced: set[str]) -> tuple[bool, str]:
    if name in referenced:
        return True, "referenced by an included API member"
    blob = (name + " " + summary + " " + values_text).casefold()
    keywords = (
        "drawing", "sheet", "view", "annotation", "dimension", "balloon", "table", "bom", "bill of materials",
        "datum", "gtol", "weld", "center mark", "layer", "line font", "paper", "print", "export", "save as",
        "dxf", "dwg", "pdf", "selection", "document type", "open document", "save error", "save option",
    )
    hits = [k for k in keywords if k in blob]
    return bool(hits), "keyword: " + ", ".join(hits[:8])


def parse_enum_values(root) -> list[tuple[str, str, str]]:
    rows = root.xpath("//*[@id='enummembersSection'][1]//tr[td]")
    out = []
    for row in rows:
        cells = row.xpath("./td")
        if len(cells) < 2:
            continue
        name = node_text(cells[0])
        description = node_text(cells[1])
        m = re.match(r"\s*([-+]?\d+(?:\.\d+)?)\s*=\s*(.*)", description)
        value = m.group(1) if m else ""
        desc = clean_text(m.group(2)) if m else description
        if name:
            out.append((name, value, desc))
    return out


def ingest_enums(
    conn: sqlite3.Connection,
    source_ids: dict[str, int],
    referenced_counts: Counter[str],
) -> dict[str, int]:
    enum_ids: dict[str, int] = {}
    source_code = "swconst"
    root_dir = LOCAL_HELP[source_code]
    for path in sorted(root_dir.glob("SolidWorks.Interop.swconst~SolidWorks.Interop.swconst.*.html")):
        if path.stat().st_size == 0:
            continue
        name = path.name.split(".")[-2]
        if not (name.endswith("_e") or name.startswith("sw")):
            continue
        root, raw = read_local_html(path)
        title = page_title(root, name)
        if "enumeration" not in title.casefold():
            continue
        summary = leading_summary_local(root)
        values = parse_enum_values(root)
        relevant, reason = enum_is_relevant(name, summary, " ".join(" ".join(v) for v in values), set(referenced_counts))
        if not relevant:
            continue
        syntax = syntax_blocks(root)
        remarks = section_text_local(root, "remarksSection", 1600)
        availability = section_text_local(root, "availabilitySection", 600)
        page_id, url = insert_source_page(conn, source_ids[source_code], source_code, path, title, "api_enum", raw)
        cur = conn.execute(
            "INSERT INTO api_enums(source_page_id,library,name,summary,remarks,syntax_vbnet,syntax_csharp,availability,"
            "referenced_by_count,drawing_relevance,official_url) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                page_id, "swconst", name, summary, remarks, syntax.get("vbnet", ""), syntax.get("csharp", ""),
                availability, referenced_counts.get(name, 0), reason, url,
            ),
        )
        enum_id = int(cur.lastrowid)
        enum_ids[name] = enum_id
        for value_name, numeric, description in values:
            conn.execute(
                "INSERT INTO api_enum_values(enum_id,name,numeric_value,description) VALUES(?,?,?,?)",
                (enum_id, value_name, numeric, description),
            )

    # Command IDs are one very large official enumeration.  Keep all members;
    # drawing-related commands are discoverable through FTS and workflow tags.
    cmd_path = LOCAL_HELP["swcommands"] / "SolidWorks.Interop.swcommands~SolidWorks.Interop.swcommands.swCommands_e.html"
    if cmd_path.exists():
        root, raw = read_local_html(cmd_path)
        title = page_title(root, "swCommands_e Enumeration")
        summary = leading_summary_local(root)
        values = parse_enum_values(root)
        page_id, url = insert_source_page(
            conn, source_ids["swcommands"], "swcommands", cmd_path, title, "api_enum", raw
        )
        cur = conn.execute(
            "INSERT INTO api_enums(source_page_id,library,name,summary,remarks,syntax_vbnet,syntax_csharp,availability,"
            "referenced_by_count,drawing_relevance,official_url) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                page_id, "swcommands", "swCommands_e", summary, section_text_local(root, "remarksSection", 1200),
                syntax_blocks(root).get("vbnet", ""), syntax_blocks(root).get("csharp", ""),
                section_text_local(root, "availabilitySection", 600), 0,
                "Complete command-ID catalog; filter values by drawing/view/sheet/table/dimension/annotation.", url,
            ),
        )
        enum_id = int(cur.lastrowid)
        enum_ids["swCommands_e"] = enum_id
        for value_name, numeric, description in values:
            conn.execute(
                "INSERT INTO api_enum_values(enum_id,name,numeric_value,description) VALUES(?,?,?,?)",
                (enum_id, value_name, numeric, description),
            )

    # Resolve member -> enum relationships now that enum ids exist.
    for name, enum_id in enum_ids.items():
        conn.execute(
            "UPDATE relationships SET to_id=? WHERE relation='uses_enum' AND to_symbol=?",
            (enum_id, name),
        )
    return enum_ids


def extract_document_outline(root, max_sections: int = 24) -> tuple[str, str]:
    body = root.xpath("//body[1]")
    if not body:
        return "", ""
    headings = []
    snippets = []
    for h in body[0].xpath(".//h1|.//h2|.//h3|.//h4"):
        title = node_text(h)
        if not title or title.casefold() in {"solidworks api help", "see also"}:
            continue
        if title not in headings:
            headings.append(title)
        if len(snippets) < 8:
            nxt = h.getnext()
            if nxt is not None:
                text = node_text(nxt)
                if text:
                    snippets.append(f"{title}: {compact_summary(text, 320)}")
        if len(headings) >= max_sections:
            break
    paragraphs = [node_text(p) for p in body[0].xpath(".//p") if node_text(p)]
    summary = compact_summary(" ".join(paragraphs[:5] + snippets), 1600)
    return summary, " | ".join(headings[:max_sections])


def ingest_programming_guide(conn: sqlite3.Connection, source_ids: dict[str, int]) -> int:
    source_code = "sldworksapiprogguide"
    root_dir = LOCAL_HELP[source_code]
    count = 0
    for path in sorted(list(root_dir.rglob("*.htm")) + list(root_dir.rglob("*.html"))):
        if path.stat().st_size == 0:
            continue
        if path.name.casefold() in {"blank.htm", "blank.html"}:
            continue
        root, raw = read_local_html(path)
        title = page_title(root, path.stem.replace("_", " "))
        if not title:
            continue
        summary, outline = extract_document_outline(root)
        category = path.relative_to(root_dir).parts[0] if len(path.relative_to(root_dir).parts) > 1 else "Overview"
        page_id, url = insert_source_page(conn, source_ids[source_code], source_code, path, title, "guide_topic", raw)
        conn.execute(
            "INSERT INTO guide_topics(source_page_id,title,category,summary,section_outline,search_terms,official_url) "
            "VALUES(?,?,?,?,?,?,?)",
            (page_id, title, category, summary, outline, task_tags(title, summary, outline), url),
        )
        count += 1
    return count


def language_from_title(title: str) -> str:
    m = re.search(r"\((VBA|VB\.NET|C#|C\+\+|C\+\+/CLI)\)", title, re.I)
    if m:
        return m.group(1).upper().replace("VB.NET", "VB.NET")
    return ""


def clean_comment_block(text: str) -> str:
    lines = []
    for line in clean_text(text).splitlines():
        line = re.sub(r"^\s*(?:'|//|/\*+|\*+|#)\s?", "", line)
        line = re.sub(r"[-=]{5,}", "", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def extract_pre_post(code_text: str) -> tuple[str, str]:
    text = clean_comment_block(code_text)
    pre = post = ""
    m = re.search(r"(?is)Preconditions?\s*:\s*(.*?)(?=Postconditions?\s*:|\Z)", text)
    if m:
        pre = compact_summary(m.group(1), 1600)
    m = re.search(r"(?is)Postconditions?\s*:\s*(.*?)(?=Option Explicit|Sub\s+main|using\s+System|Imports\s+|\Z)", text)
    if m:
        post = compact_summary(m.group(1), 1600)
    return pre, post


def extract_example_calls(root) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    pres = root.xpath("//pre")
    code = "\n".join(node_text(p) for p in pres)
    types = []
    for m in re.finditer(r"(?i)\b(?:SldWorks|SolidWorks\.Interop\.sldworks)\.([A-Z][A-Za-z0-9_]*)", code):
        name = m.group(1)
        if name not in types:
            types.append(name)
    calls = []
    typed_calls: list[tuple[str, str]] = []
    for b in root.xpath("//pre//b|//pre//strong"):
        name = node_text(b).strip()
        name = name.split("(", 1)[0].strip()
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name) and name not in STOP_CALLS:
            if name not in calls:
                calls.append(name)
            typed_calls.append(("", name))
    for m in re.finditer(r"\.([A-Z][A-Za-z0-9_]{2,})\b", code):
        name = m.group(1)
        if name not in STOP_CALLS and name not in calls:
            calls.append(name)
            typed_calls.append(("", name))
    # Interface-qualified calls in prose or comments.
    body_text = node_text(root)
    for m in re.finditer(r"\b(I[A-Z][A-Za-z0-9_]*)::([A-Z][A-Za-z0-9_]*)", body_text):
        pair = (m.group(1), m.group(2))
        if pair[1] not in calls:
            calls.append(pair[1])
        if pair not in typed_calls:
            typed_calls.append(pair)
    return types, calls, typed_calls


def example_objective(root, title: str) -> str:
    for p in root.xpath("//body//p"):
        text = node_text(p)
        if text and text != title and not text.casefold().startswith("solidworks api help"):
            if "this example" in text.casefold() or len(text) > 25:
                return compact_summary(text, 1000)
    return ""


def ingest_examples(
    conn: sqlite3.Connection,
    source_ids: dict[str, int],
    linked_names: set[str],
) -> int:
    type_rows = conn.execute("SELECT id,name,display_name FROM api_types WHERE library='sldworks'").fetchall()
    drawing_type_names = {row[1] for row in type_rows if row[1] in DRAWING_INTERFACES}
    display_names = {row[2] for row in type_rows if row[1] in DRAWING_INTERFACES and row[2]}
    member_rows = conn.execute(
        "SELECT m.id,t.name,m.name FROM api_members m JOIN api_types t ON t.id=m.type_id"
    ).fetchall()
    by_member: defaultdict[str, list[tuple[int, str]]] = defaultdict(list)
    exact_member: dict[tuple[str, str], int] = {}
    for member_id, type_name, member_name in member_rows:
        by_member[member_name].append((member_id, type_name))
        exact_member[(type_name, member_name)] = member_id

    count = 0
    example_id_by_file: dict[str, int] = {}
    for source_code in ("sldworksapi", "swdocmgrapi"):
        root_dir = LOCAL_HELP[source_code]
        paths = sorted(list(root_dir.rglob("*.htm")) + list(root_dir.rglob("*.html")))
        for path in paths:
            if path.stat().st_size == 0:
                continue
            low_name = path.name.casefold()
            if "example" not in low_name:
                continue
            root, raw = read_local_html(path)
            title = page_title(root, path.stem.replace("_", " "))
            if "example" not in title.casefold():
                continue
            objective = example_objective(root, title)
            pres = root.xpath("//pre")
            code_text = "\n".join(node_text(p) for p in pres)
            pre, post = extract_pre_post(code_text)
            types, calls, typed_calls = extract_example_calls(root)
            relevance_blob = (title + " " + objective + " " + pre + " " + post).casefold()
            title_hits = [kw for kw in DRAWING_KEYWORDS if kw in (title + " " + objective).casefold()]
            body_hits = [kw for kw in DRAWING_KEYWORDS if kw in relevance_blob]
            type_hits = [t for t in types if t in drawing_type_names or t in display_names]
            linked = low_name in linked_names
            relevant = linked or bool(title_hits) or bool(type_hits) or len(body_hits) >= 2
            if source_code == "swdocmgrapi" and not re.search(r"(?i)(drawing|sheet|view|preview|reference)", relevance_blob):
                relevant = False
            if not relevant:
                continue
            reason_parts = []
            if linked:
                reason_parts.append("linked from an included official API page")
            if title_hits:
                reason_parts.append("drawing terms: " + ", ".join(title_hits[:8]))
            if type_hits:
                reason_parts.append("drawing API types: " + ", ".join(type_hits[:8]))
            side_effect = sentences_containing(
                post + " " + objective,
                ("overwrite", "delete", "save", "create", "close", "change"),
                900,
            )
            page_id, url = insert_source_page(
                conn, source_ids[source_code], source_code, path, title, "example", raw
            )
            cur = conn.execute(
                "INSERT INTO examples(source_page_id,title,language,objective,prerequisites,postconditions,api_types_json,"
                "api_calls_json,destructive_side_effects,relevance_reason,search_terms,official_url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    page_id, title, language_from_title(title), objective, pre, post,
                    json.dumps(types, ensure_ascii=False), json.dumps(calls, ensure_ascii=False), side_effect,
                    "; ".join(reason_parts), task_tags(title, objective, pre + " " + post), url,
                ),
            )
            example_id = int(cur.lastrowid)
            example_id_by_file[low_name] = example_id
            for seq, (explicit_type, call) in enumerate(typed_calls, 1):
                resolved = exact_member.get((explicit_type, call)) if explicit_type else None
                inferred_type = explicit_type
                if resolved is None and len(by_member.get(call, [])) == 1:
                    resolved, inferred_type = by_member[call][0]
                conn.execute(
                    "INSERT OR IGNORE INTO example_calls(example_id,sequence,api_type,api_member,resolved_member_id) VALUES(?,?,?,?,?)",
                    (example_id, seq, inferred_type, call, resolved),
                )
                if resolved:
                    conn.execute(
                        "INSERT OR IGNORE INTO relationships(from_kind,from_id,relation,to_kind,to_id,to_symbol,evidence_url) "
                        "VALUES('example',?,'calls','api_member',?,?,?)",
                        (example_id, resolved, f"{inferred_type}::{call}" if inferred_type else call, url),
                    )
            count += 1

    # Recompute example_count using resolved calls, while preserving direct
    # official links recorded on member pages if they are greater.
    conn.execute(
        "UPDATE api_members SET example_count=MAX(example_count, "
        "(SELECT COUNT(DISTINCT ec.example_id) FROM example_calls ec WHERE ec.resolved_member_id=api_members.id))"
    )
    return count


def resolve_symbol_url(conn: sqlite3.Connection, symbol: str) -> str:
    if "::" in symbol:
        type_name, member_name = symbol.split("::", 1)
        row = conn.execute(
            "SELECT m.official_url FROM api_members m JOIN api_types t ON t.id=m.type_id WHERE t.name=? AND m.name=?",
            (type_name, member_name),
        ).fetchone()
    else:
        row = conn.execute("SELECT official_url FROM api_types WHERE name=?", (symbol,)).fetchone()
    return row[0] if row else ""


def curated_workflows() -> list[dict[str, Any]]:
    """Original task-oriented synthesis over the official API records.

    The steps intentionally name calls and required state, but do not copy
    official example source code.  Symbol existence is validated after insert.
    """
    return [
        {
            "slug": "macro-bootstrap-vba", "title": "Start a robust VBA drawing macro", "category": "macro-runtime",
            "goal": "Attach to the running SOLIDWORKS session, validate the active document, and obtain drawing interfaces safely.",
            "recommended": "Interactive or recorded .swp macros launched from SOLIDWORKS.",
            "avoid": "Use an add-in or standalone executable when long-running UI integration or deployment is required.",
            "prereq": "SOLIDWORKS is running; the macro references the SOLIDWORKS type library and constants library.",
            "steps": [
                ("Obtain the application object from Application.SldWorks.", "ISldWorks::ActiveDoc", "A live ISldWorks dispatch object is available."),
                ("Read ActiveDoc and stop with an explicit message when it is Nothing.", "ISldWorks::ActiveDoc", "A document object is available."),
                ("Check IModelDoc2.GetType against swDocDRAWING before casting to DrawingDoc.", "IModelDoc2::GetType|swDocumentTypes_e", "The macro never calls drawing-only methods on a part or assembly."),
                ("Acquire IDrawingDoc plus IModelDocExtension and ISelectionMgr from the active model.", "IDrawingDoc|IModelDoc2::Extension|IModelDoc2::SelectionManager", "All document-level helpers are ready."),
                ("Use structured error handling and restore any user preferences changed by the macro.", "ISldWorks::GetUserPreferenceToggle|ISldWorks::SetUserPreferenceToggle", "Failures leave SOLIDWORKS state predictable."),
            ],
            "verify": "Confirm document type, non-Nothing interfaces, and no changed preference after an intentional early exit.",
            "pitfalls": "VBA API lengths are meters; a drawing document also implements common IModelDoc2 operations; recorded macros often omit validation.",
            "chain": "Application.SldWorks -> ActiveDoc -> GetType -> DrawingDoc/Extension/SelectionManager",
            "terms": "VBA macro شروع ماکرو اتصال سالیدورکس ActiveDoc نقشه drawing bootstrap error handling",
            "symbols": ["ISldWorks::ActiveDoc", "IModelDoc2::GetType", "IModelDoc2::Extension", "IModelDoc2::SelectionManager"],
        },
        {
            "slug": "create-drawing-from-template", "title": "Create a drawing from a template", "category": "document-lifecycle",
            "goal": "Create a new SLDDrw document with a known sheet format and predictable units.",
            "recommended": "Batch drawing generation and template-controlled deliverables.",
            "avoid": "Do not guess a template path; resolve a site-approved .drwdot first.",
            "prereq": "An existing readable .drwdot template path and known projection/standard policy.",
            "steps": [
                ("Resolve and validate the drawing template path before calling the API.", "ISldWorks::GetUserPreferenceStringValue", "A real .drwdot file is selected."),
                ("Call ISldWorks.NewDocument with the drawing template.", "ISldWorks::NewDocument", "A new drawing ModelDoc2 is returned."),
                ("Check the returned document type and cast to IDrawingDoc.", "IModelDoc2::GetType|IDrawingDoc", "Drawing-only operations are safe."),
                ("Read the current ISheet properties and set sheet scale, size, and format only when the template does not already enforce them.", "IDrawingDoc::GetCurrentSheet|ISheet::GetProperties2|IDrawingDoc::SetupSheet6", "Sheet settings match the requested specification."),
                ("Force a rebuild and save with explicit error and warning outputs.", "IModelDoc2::ForceRebuild3|IModelDocExtension::SaveAs3", "A persisted SLDDrw exists and errors are captured."),
            ],
            "verify": "Check active document type, sheet name/size/scale, SaveAs errors, and that the output file opens.",
            "pitfalls": "NewDocument returns a generic ModelDoc2; template paths and sheet format paths are separate; save errors are bitmasks.",
            "chain": "NewDocument -> GetCurrentSheet -> GetProperties2/SetupSheet6 -> ForceRebuild3 -> SaveAs3",
            "terms": "ساخت نقشه از تمپلیت قالب drwdot شیت sheet format new drawing template",
            "symbols": ["ISldWorks::NewDocument", "IDrawingDoc::GetCurrentSheet", "IDrawingDoc::SetupSheet6", "IModelDocExtension::SaveAs3"],
        },
        {
            "slug": "open-existing-drawing", "title": "Open an existing drawing safely", "category": "document-lifecycle",
            "goal": "Open an SLDDrw with controlled configuration, read-only, silent, and error behavior.",
            "recommended": "Batch updates, inspection, export, or repair of existing drawings.",
            "avoid": "Use Document Manager when only metadata/references/previews are needed and SOLIDWORKS need not load the model.",
            "prereq": "A normalized SLDDrw path and a decision about silent/read-only behavior.",
            "steps": [
                ("Build an IDocumentSpecification and set file name, document type, read-only, and silent options when OpenDoc7 is available.", "ISldWorks::OpenDoc7", "Open options are explicit."),
                ("Open the file and capture error/warning status; use OpenDoc6 only for compatibility with older automation.", "ISldWorks::OpenDoc6|ISldWorks::OpenDoc7", "A ModelDoc2 or an actionable failure is returned."),
                ("Verify GetType is drawing and inspect IsOpenedReadOnly before editing.", "IModelDoc2::GetType|IModelDoc2::IsOpenedReadOnly", "The macro knows whether writes are permitted."),
                ("Activate the document explicitly before UI-dependent selection or commands.", "ISldWorks::ActivateDoc3", "The intended drawing owns subsequent UI operations."),
            ],
            "verify": "Log the returned document, error/warning codes, read-only state, and active title.",
            "pitfalls": "Silent open does not make all referenced-model failures harmless; UI-dependent calls can target the wrong active document.",
            "chain": "OpenDoc7/OpenDoc6 -> GetType -> IsOpenedReadOnly -> ActivateDoc3",
            "terms": "باز کردن نقشه موجود slddrw read only silent OpenDoc error warning",
            "symbols": ["ISldWorks::OpenDoc7", "ISldWorks::OpenDoc6", "IModelDoc2::IsOpenedReadOnly", "ISldWorks::ActivateDoc3"],
        },
        {
            "slug": "manage-sheets", "title": "Create, activate, rename, and configure sheets", "category": "sheets",
            "goal": "Manage a multi-sheet drawing without confusing sheet views with model views.",
            "recommended": "Multi-page production drawings and batch sheet generation.",
            "avoid": "Do not traverse sheet names by assuming Sheet1, Sheet2 naming.",
            "prereq": "Active drawing and an approved sheet format/template policy.",
            "steps": [
                ("Read sheet names from IDrawingDoc.GetSheetNames and activate the intended sheet.", "IDrawingDoc::GetSheetNames|IDrawingDoc::ActivateSheet", "The target sheet is current."),
                ("Create a sheet with the newest supported NewSheet overload and explicit paper/scale/template arguments.", "IDrawingDoc::NewSheet4", "A new sheet is present."),
                ("Get the ISheet object and inspect name, properties, and page setup.", "IDrawingDoc::GetCurrentSheet|ISheet::GetName|ISheet::GetProperties2|ISheet::PageSetup", "Actual sheet state is known."),
                ("Apply SetupSheet6 only when properties differ; keep scale numerator/denominator explicit.", "IDrawingDoc::SetupSheet6", "Sheet size, scale, projection, and format are deterministic."),
                ("Use GetFirstView carefully: its first returned view is the sheet-format view, not a model view.", "IDrawingDoc::GetFirstView|IView::GetNextView", "Traversal does not treat the sheet as a model view."),
            ],
            "verify": "Re-read GetSheetNames, current sheet name, ISheet properties, and sheet-format path.",
            "pitfalls": "The first IView on a sheet represents the sheet; sheet scale uses numerator and denominator; template and format options differ.",
            "chain": "GetSheetNames -> ActivateSheet/NewSheet4 -> GetCurrentSheet -> SetupSheet6",
            "terms": "مدیریت شیت صفحه نقشه sheet create rename activate NewSheet SetupSheet کادر",
            "symbols": ["IDrawingDoc::GetSheetNames", "IDrawingDoc::ActivateSheet", "IDrawingDoc::NewSheet4", "IDrawingDoc::SetupSheet6", "IDrawingDoc::GetFirstView"],
        },
        {
            "slug": "create-standard-views", "title": "Create first-angle or third-angle standard views", "category": "views",
            "goal": "Insert a coordinated orthographic view set from a model.",
            "recommended": "Fast standard projection drawings where default placement is acceptable as a starting point.",
            "avoid": "Use CreateDrawViewFromModelView3 when exact named-view placement is required.",
            "prereq": "Saved part/assembly path, active empty drawing sheet, chosen projection convention.",
            "steps": [
                ("Confirm the sheet projection convention and model path.", "ISheet::GetProperties2|IModelDoc2::GetPathName", "Projection and source are explicit."),
                ("Call Create1stAngleViews2 or Create3rdAngleViews2 with the full model path.", "IDrawingDoc::Create1stAngleViews2|IDrawingDoc::Create3rdAngleViews2", "Standard orthographic views are inserted."),
                ("Traverse from GetFirstView/GetNextView and record names, types, positions, and scales.", "IDrawingDoc::GetFirstView|IView::GetNextView|IView::GetName2|IView::Type|IView::Position|IView::ScaleDecimal", "Inserted views are measured."),
                ("Reposition or align views only after discovering their actual names and parent relationships.", "IView::Position|IDrawingDoc::AlignHorz|IDrawingDoc::AlignVert", "Layout is controlled."),
            ],
            "verify": "Count non-sheet views, confirm view types and projection, check overlap and sheet bounds, rebuild.",
            "pitfalls": "Methods depend on the active sheet; the source path must include the model extension; automatic scaling can change sheet/view scale.",
            "chain": "Create1stAngleViews2/Create3rdAngleViews2 -> GetFirstView/GetNextView -> Position/Align",
            "terms": "سه نما نمای استاندارد first angle third angle ارتوگرافیک projection",
            "symbols": ["IDrawingDoc::Create1stAngleViews2", "IDrawingDoc::Create3rdAngleViews2", "IView::Position"],
        },
        {
            "slug": "create-named-model-view", "title": "Create a drawing view from a named model view", "category": "views",
            "goal": "Place a specific standard or custom model view at exact sheet coordinates.",
            "recommended": "Controlled layouts, isometric views, and custom named views.",
            "avoid": "Do not use guessed view names; standard view names include the leading asterisk.",
            "prereq": "Full model path, exact model-view name, and insertion coordinates in meters.",
            "steps": [
                ("Obtain the full saved model path; include .sldprt or .sldasm.", "IModelDoc2::GetPathName", "ModelName is valid."),
                ("Choose the exact named view such as *Front or *Isometric and convert layout coordinates from mm to meters.", "IDrawingDoc::CreateDrawViewFromModelView3", "Arguments use API units."),
                ("Call CreateDrawViewFromModelView3 and require a non-Nothing IView return.", "IDrawingDoc::CreateDrawViewFromModelView3", "A drawing view exists."),
                ("Set scale/position through IView properties if automatic scaling changed the result.", "IView::UseSheetScale|IView::ScaleDecimal|IView::Position", "Final placement and scale are explicit."),
            ],
            "verify": "Check returned IView, referenced document/configuration, view name/type, center position, and sheet bounds.",
            "pitfalls": "All Loc coordinates are meters; the named view must match exactly; automatic scaling preference can alter the initial scale.",
            "chain": "GetPathName -> CreateDrawViewFromModelView3 -> UseSheetScale/ScaleDecimal/Position",
            "terms": "ساخت نما از مدل named view Front Isometric موقعیت مختصات متر",
            "symbols": ["IDrawingDoc::CreateDrawViewFromModelView3", "IView::UseSheetScale", "IView::ScaleDecimal", "IView::Position"],
        },
        {
            "slug": "create-projected-view", "title": "Create a projected view", "category": "views",
            "goal": "Create a projection derived from an existing parent drawing view.",
            "recommended": "Orthographically related layouts that must remain aligned to a parent.",
            "avoid": "Do not rely on screen clicks when a parent IView can be selected deterministically.",
            "prereq": "An existing parent IView and target sheet coordinates in meters.",
            "steps": [
                ("Activate the sheet and select the parent drawing view with the documented view selection type.", "IDrawingDoc::ActivateSheet|IView::SelectEntity|IModelDocExtension::SelectByID2", "The parent view is the only intended selection."),
                ("Create the projected/unfolded view at the target location using the current API overload.", "IDrawingDoc::CreateUnfoldedViewAt3", "A derived IView is returned/created."),
                ("Read the new view through the view chain and verify its parent/alignment.", "IDrawingDoc::GetFirstView|IView::GetNextView|IView::GetBaseView", "The projection is linked to the intended parent."),
                ("Adjust Position without breaking the intended alignment policy.", "IView::Position|IView::AlignWithView", "Layout is acceptable."),
            ],
            "verify": "Confirm base-view relationship, projection direction, scale inheritance, and no overlap.",
            "pitfalls": "Selection state is part of the method contract; sheet view is first in traversal; alignment may constrain movement.",
            "chain": "Select parent view -> CreateUnfoldedViewAt3 -> GetBaseView -> Position/AlignWithView",
            "terms": "نمای پروجکت projected view نمای مشتق aligned parent",
            "symbols": ["IDrawingDoc::CreateUnfoldedViewAt3", "IView::GetBaseView", "IView::AlignWithView"],
        },
        {
            "slug": "create-section-view", "title": "Create a section view", "category": "views",
            "goal": "Create a section line and derived section view with explicit options.",
            "recommended": "Internal geometry documentation and standards-compliant section views.",
            "avoid": "Do not call the creation method with stale sketch/edge selections.",
            "prereq": "Parent view active, section sketch/line geometry defined, section options chosen.",
            "steps": [
                ("Activate the parent view and clear previous selections.", "IDrawingDoc::ActivateView|IModelDoc2::ClearSelection2", "Only the target view is active."),
                ("Create/select the section line geometry in drawing coordinates.", "ISketchManager::CreateLine|IModelDocExtension::SelectByID2", "A valid cutting line is selected."),
                ("Call CreateSectionViewAt5 with label, placement, options, excluded components, and explicit section depth.", "IDrawingDoc::CreateSectionViewAt5|swCreateSectionViewAtOptions_e", "A section view is created."),
                ("Inspect IView.Type, position, scale, referenced configuration, and section data.", "IView::Type|IView::Position|IView::ScaleDecimal", "Derived view state matches intent."),
            ],
            "verify": "Check section arrows/label, hatch, cut direction, excluded components, alignment, and rebuild status.",
            "pitfalls": "Coordinates are meters; creation depends on active view and selected section geometry; option flags are combinable bit values.",
            "chain": "ActivateView -> create/select section line -> CreateSectionViewAt5 -> inspect IView",
            "terms": "نمای برش section view خط برش hatch cutting line",
            "symbols": ["IDrawingDoc::ActivateView", "IDrawingDoc::CreateSectionViewAt5", "swCreateSectionViewAtOptions_e"],
        },
        {
            "slug": "create-detail-view", "title": "Create a detail view", "category": "views",
            "goal": "Create a bounded detail view from a parent drawing view.",
            "recommended": "Magnified local geometry and crowded feature callouts.",
            "avoid": "Do not omit the detail boundary or assume the returned scale.",
            "prereq": "Parent view active and a closed detail boundary sketch selected.",
            "steps": [
                ("Activate the parent IView and create/select a closed circular or profile boundary.", "IDrawingDoc::ActivateView|ISketchManager::CreateCircleByRadius", "A detail boundary exists in the view."),
                ("Call the newest CreateDetailViewAt overload available, normally CreateDetailViewAt4.", "IDrawingDoc::CreateDetailViewAt4", "A detail IView is created."),
                ("Set or verify label, position, scale, and display properties on the new view.", "IView::Position|IView::ScaleDecimal|IView::GetName2", "Detail presentation matches the drawing standard."),
            ],
            "verify": "Check boundary attachment, label uniqueness, scale note, sheet placement, and parent reference.",
            "pitfalls": "Older CreateDetailView variants are obsolete; boundary selection and active-view state are required.",
            "chain": "ActivateView -> boundary sketch -> CreateDetailViewAt4 -> Position/ScaleDecimal",
            "terms": "نمای جزئیات detail view بزرگنمایی دایره مرز scale",
            "symbols": ["IDrawingDoc::CreateDetailViewAt4", "ISketchManager::CreateCircleByRadius", "IView::ScaleDecimal"],
        },
        {
            "slug": "create-auxiliary-view", "title": "Create an auxiliary view", "category": "views",
            "goal": "Create a true-size view normal to a selected inclined edge.",
            "recommended": "Inclined faces/features that are foreshortened in principal views.",
            "avoid": "Do not select an ambiguous or non-linear entity for direction.",
            "prereq": "Parent view and a deterministic selected edge defining auxiliary direction.",
            "steps": [
                ("Activate the parent view and select the intended edge in that view.", "IDrawingDoc::ActivateView|IEntity::Select4", "The direction edge is selected."),
                ("Call CreateAuxiliaryViewAt2 with target center and label/options.", "IDrawingDoc::CreateAuxiliaryViewAt2", "An auxiliary view is created."),
                ("Inspect alignment, label, scale, and position; move only along allowed alignment if required.", "IView::Position|IView::GetBaseView|IView::ScaleDecimal", "View is correctly derived and placed."),
            ],
            "verify": "Confirm selected edge, projection direction, parent view, true-size orientation, and sheet bounds.",
            "pitfalls": "The selected edge controls orientation; selection must belong to the intended parent view.",
            "chain": "ActivateView -> Select4(edge) -> CreateAuxiliaryViewAt2 -> inspect IView",
            "terms": "نمای کمکی auxiliary view سطح شیبدار edge true size",
            "symbols": ["IDrawingDoc::CreateAuxiliaryViewAt2", "IEntity::Select4", "IView::GetBaseView"],
        },
        {
            "slug": "create-flat-pattern-view", "title": "Create a sheet-metal flat-pattern view", "category": "views",
            "goal": "Insert a flat-pattern drawing view with bend information from a sheet-metal part.",
            "recommended": "Manufacturing drawings for sheet-metal blanks.",
            "avoid": "Do not use on a model/configuration without a resolvable flat pattern.",
            "prereq": "Saved sheet-metal part, configuration choice, and placement/rotation policy.",
            "steps": [
                ("Resolve the full sheet-metal model path and target configuration.", "IModelDoc2::GetPathName", "Source is unambiguous."),
                ("Call CreateFlatPatternViewFromModelView3 with model, configuration/view, coordinates, and flip/rotation options.", "IDrawingDoc::CreateFlatPatternViewFromModelView3", "A flat-pattern view is inserted."),
                ("Check IsFlatPatternView and referenced configuration, then set bend-note/visibility policies.", "IView::IsFlatPatternView|IView::ReferencedConfiguration", "The view is a true flat pattern."),
                ("Verify scale, rotation, bend lines, bounding box, and export requirements.", "IView::ScaleDecimal|IView::Position", "Manufacturing layout is usable."),
            ],
            "verify": "Require IsFlatPatternView=True, correct configuration, visible bend lines/notes as specified, and no overlap.",
            "pitfalls": "Configuration and flat-pattern state matter; obsolete overloads exist; rotation and flip flags can invert orientation.",
            "chain": "GetPathName -> CreateFlatPatternViewFromModelView3 -> IsFlatPatternView -> scale/position",
            "terms": "نمای بازشده ورقکاری flat pattern sheet metal bend lines unfolded",
            "symbols": ["IDrawingDoc::CreateFlatPatternViewFromModelView3", "IView::IsFlatPatternView", "IView::ReferencedConfiguration"],
        },
        {
            "slug": "layout-views", "title": "Traverse, measure, position, scale, and align views", "category": "views",
            "goal": "Produce a deterministic non-overlapping sheet layout after view creation.",
            "recommended": "Every automated drawing layout.",
            "avoid": "Do not identify views only by traversal index across different templates/configurations.",
            "prereq": "Active sheet with created views and known sheet dimensions.",
            "steps": [
                ("Traverse GetFirstView/GetNextView; skip the initial sheet view and record stable names/types.", "IDrawingDoc::GetFirstView|IView::GetNextView|IView::GetName2|IView::Type", "All model views are cataloged."),
                ("Read each view outline/bounding box, Position, ScaleDecimal, and UseSheetScale.", "IView::GetOutline|IView::Position|IView::ScaleDecimal|IView::UseSheetScale", "Current extents are known in meters."),
                ("Choose scale before final placement, because scale changes the outline.", "IView::ScaleDecimal|IView::UseSheetScale", "Final extents are stable."),
                ("Set Position and align derived views with AlignHorz/AlignVert or IView.AlignWithView.", "IView::Position|IDrawingDoc::AlignHorz|IDrawingDoc::AlignVert|IView::AlignWithView", "Projection relationships and spacing are controlled."),
                ("Recompute outlines and test them against sheet margins and other view rectangles.", "IView::GetOutline|ISheet::GetProperties2", "No view or annotation zone exceeds limits."),
            ],
            "verify": "Pairwise rectangle overlap test, sheet-margin test, scale policy, alignment relationships, and visual rebuild.",
            "pitfalls": "Position is view center; GetOutline is in sheet coordinates/meters; automatic or sheet scale changes can invalidate earlier placement.",
            "chain": "GetFirstView/GetNextView -> GetOutline -> ScaleDecimal -> Position -> Align",
            "terms": "چیدمان نما position scale align overlap bounding box فاصله شیت",
            "symbols": ["IView::GetOutline", "IView::Position", "IView::ScaleDecimal", "IView::AlignWithView"],
        },
        {
            "slug": "insert-model-items", "title": "Insert model dimensions and annotations", "category": "dimensions",
            "goal": "Import appropriate model items into selected drawing views or the entire drawing.",
            "recommended": "Models with disciplined design dimensions and annotation views.",
            "avoid": "Avoid blind import when it produces duplicated or unreadable dimensions.",
            "prereq": "Drawing views reference rebuilt models; import scope and mark/options are chosen.",
            "steps": [
                ("Activate/select the target view or choose all-views scope explicitly.", "IDrawingDoc::ActivateView|IModelDocExtension::SelectByID2", "Import scope is deterministic."),
                ("Call InsertModelAnnotations3 with source/type/options and mark filters.", "IDrawingDoc::InsertModelAnnotations3", "Model items are inserted."),
                ("Traverse returned/visible display dimensions and annotations to detect duplicates and hidden items.", "IView::GetFirstDisplayDimension6|IDisplayDimension::GetNext5|IView::GetAnnotations", "Imported items are inventoried."),
                ("Reposition, hide, or delete only confirmed unwanted items using persistent identification where possible.", "IAnnotation::SetPosition2|IAnnotation::Visible|IModelDocExtension::DeleteSelection2", "The sheet is readable."),
            ],
            "verify": "Count imported items, check dangling state, duplicate dimensions, overlap, and view association.",
            "pitfalls": "Selection and option masks control scope; inserted items can be redundant or outside view bounds; return arrays can be empty.",
            "chain": "select/activate view -> InsertModelAnnotations3 -> traverse DisplayDimension/Annotation -> clean layout",
            "terms": "وارد کردن ابعاد مدل insert model items annotations dimensions import",
            "symbols": ["IDrawingDoc::InsertModelAnnotations3", "IView::GetFirstDisplayDimension6", "IDisplayDimension::GetNext5", "IAnnotation::SetPosition2"],
        },
        {
            "slug": "create-dimensions", "title": "Create drawing dimensions and tolerances", "category": "dimensions",
            "goal": "Add associative dimensions to selected drawing entities and format their tolerance/display.",
            "recommended": "Dimensions not imported from model items or intentionally placed by automation.",
            "avoid": "Non-associative legacy Create*Dim calls are usually inferior to entity selection plus AddDimension2.",
            "prereq": "Target drawing entities are selected in the correct order and placement coordinates are converted to meters.",
            "steps": [
                ("Clear selection and select view entities using IEntity.Select4 or a stable SelectByID2 specification.", "IModelDoc2::ClearSelection2|IEntity::Select4|IModelDocExtension::SelectByID2", "The intended geometry is selected."),
                ("Call AddDimension2 or the horizontal/vertical/radial variant at the desired sheet position.", "IModelDoc2::AddDimension2|IModelDoc2::AddHorizontalDimension2|IModelDoc2::AddVerticalDimension2|IModelDoc2::AddRadialDimension2", "A DisplayDimension is returned."),
                ("Get IDimension/IDimensionTolerance and apply text, precision, tolerance type, fit, or limits.", "IDisplayDimension::GetDimension2|IDimension::Tolerance|IDimensionTolerance::SetValues2", "Value presentation matches the specification."),
                ("Use IAnnotation for leader/text position and verify the dimension is attached and not dangling.", "IDisplayDimension::GetAnnotation|IAnnotation::SetPosition2|IDisplayDimension::GetDisplayData", "Dimension is readable and associated."),
            ],
            "verify": "Check returned objects, system value, displayed precision/tolerance, association, dangling state, and overlap.",
            "pitfalls": "Selection order changes dimension type; coordinates and system values use SI units; some legacy drawing dimension calls create non-associative dimensions.",
            "chain": "select entities -> AddDimension2 -> GetDimension2/Tolerance -> Annotation position",
            "terms": "اندازه گذاری ابعاد tolerance تلرانس dimension associative precision fit",
            "symbols": ["IModelDoc2::AddDimension2", "IDisplayDimension::GetDimension2", "IDimension::Tolerance", "IDimensionTolerance::SetValues2"],
        },
        {
            "slug": "ordinate-dimensions", "title": "Create ordinate dimensions", "category": "dimensions",
            "goal": "Build a baseline/ordinate dimension group from selected geometry.",
            "recommended": "Machining drawings with common origin and reduced tolerance stack-up.",
            "avoid": "Do not mix origin/axis selections or add members without an active ordinate group.",
            "prereq": "Origin entity and dimensioned entities selected in documented order.",
            "steps": [
                ("Select the ordinate origin and target entities deterministically.", "IEntity::Select4|IModelDocExtension::SelectByID2", "Selection order matches the ordinate contract."),
                ("Use IModelDocExtension.AddOrdinateDimension and inspect the swCreateOrdDimError_e result.", "IModelDocExtension::AddOrdinateDimension|swCreateOrdDimError_e", "The ordinate group is created or an exact error is known."),
                ("Add additional items, align the group, and change ordinate direction only when required.", "IDrawingDoc::AlignOrdinate|IDrawingDoc::ChangeOrdDir", "Group layout is consistent."),
            ],
            "verify": "Validate origin, direction, each selected entity, error enum, alignment, and displayed units/tolerance.",
            "pitfalls": "Selection order is critical; error return is an enum; legacy IDrawingDoc ordinate methods are obsolete.",
            "chain": "select origin/entities -> IModelDocExtension.AddOrdinateDimension -> AlignOrdinate/ChangeOrdDir",
            "terms": "ابعاد مختصاتی ordinate dimension مبدا baseline coordinate",
            "symbols": ["IModelDocExtension::AddOrdinateDimension", "swCreateOrdDimError_e", "IDrawingDoc::AlignOrdinate"],
        },
        {
            "slug": "hole-callouts-center-marks", "title": "Add hole callouts, center marks, and centerlines", "category": "annotations",
            "goal": "Annotate holes and axes using associative drawing objects.",
            "recommended": "Machined parts, patterns, circular features, and hole tables.",
            "avoid": "Do not use obsolete AddCenterMark or AddHoleCallout overloads when current methods exist.",
            "prereq": "Hole edge/feature or relevant drawing entities selected in the owning view.",
            "steps": [
                ("Select the hole edge for a callout in the target view and call AddHoleCallout2 at a meter-based position.", "IDrawingDoc::AddHoleCallout2|IEntity::Select4", "An associative hole callout is created."),
                ("Create center marks with InsertCenterMark3 and inspect returned center-mark objects.", "IDrawingDoc::InsertCenterMark3", "Centers are annotated."),
                ("Create centerlines through the current centerline insertion methods or drawing entity selection.", "IDrawingDoc::InsertCenterLine2|ICenterLine", "Axes are represented."),
                ("Use IAnnotation position/layer/visibility to format each item.", "IAnnotation::SetPosition2|IAnnotation::Layer|IAnnotation::Visible", "Symbols meet sheet layout rules."),
            ],
            "verify": "Check association to correct edge/view, callout text, mark grouping, layer, visibility, and rebuild stability.",
            "pitfalls": "Obsolete overloads remain documented; selection must be in drawing context; placement coordinates are meters.",
            "chain": "select hole/entity -> AddHoleCallout2/InsertCenterMark3/InsertCenterLine2 -> format Annotation",
            "terms": "کال اوت سوراخ علامت مرکز خط مرکز hole callout center mark centerline",
            "symbols": ["IDrawingDoc::AddHoleCallout2", "IDrawingDoc::InsertCenterMark3", "IDrawingDoc::InsertCenterLine2", "IAnnotation::Layer"],
        },
        {
            "slug": "notes-leaders-balloons", "title": "Create notes, leaders, and balloons", "category": "annotations",
            "goal": "Place general notes and BOM-linked balloons with controlled text and leaders.",
            "recommended": "Manufacturing notes, callouts, and assembly item identification.",
            "avoid": "Do not hard-code item numbers that should be linked to a BOM.",
            "prereq": "Target view/entity selection and required text/property-link syntax.",
            "steps": [
                ("Insert a note through IModelDoc2.InsertNote and require an INote return.", "IModelDoc2::InsertNote|INote", "A note object exists."),
                ("Get its IAnnotation and set position, layer, text format, and leader style.", "INote::GetAnnotation|IAnnotation::SetPosition2|IAnnotation::Layer|IAnnotation::SetTextFormat", "Note presentation is controlled."),
                ("For balloons, select the target component/entity and create balloon options before insertion or auto-ballooning.", "IDrawingDoc::CreateAutoBalloonOptions|IAutoBalloonOptions|IDrawingDoc::AutoBalloon5", "Balloon behavior is explicit."),
                ("Use stack/magnetic-line APIs when a grouped layout is required.", "IBalloonStack|IMagneticLine", "Balloons are organized."),
            ],
            "verify": "Check note text/property resolution, attachment, leader endpoint, balloon item number/BOM link, and overlap.",
            "pitfalls": "A note and its annotation are different interfaces; linked property syntax and BOM association must remain intact.",
            "chain": "InsertNote -> GetAnnotation -> format; or CreateAutoBalloonOptions -> AutoBalloon5 -> stack/magnetic line",
            "terms": "یادداشت لیدر بالون note leader balloon شماره آیتم BOM",
            "symbols": ["IModelDoc2::InsertNote", "INote::GetAnnotation", "IDrawingDoc::CreateAutoBalloonOptions", "IDrawingDoc::AutoBalloon5"],
        },
        {
            "slug": "bom-and-tables", "title": "Insert and populate BOM and drawing tables", "category": "tables-bom",
            "goal": "Create associative BOM, general, hole, weldment, or revision tables and control their placement/content.",
            "recommended": "Assembly drawings, cut lists, hole schedules, and controlled tabular data.",
            "avoid": "Do not manipulate table cells before confirming the specialized annotation type and dimensions.",
            "prereq": "Target view/configuration, table template, anchor/position, and BOM/table type chosen.",
            "steps": [
                ("Activate/select the target drawing view for model-dependent tables.", "IDrawingDoc::ActivateView|IModelDocExtension::SelectByID2", "Table source context is explicit."),
                ("Insert the table with the newest appropriate method, such as InsertBomTable6 or InsertTableAnnotation2.", "IView::InsertBomTable6|IDrawingDoc::InsertTableAnnotation2|IModelDocExtension::InsertGeneralTableAnnotation", "A specialized table annotation is returned."),
                ("Access the base ITableAnnotation to inspect row/column counts, text, merged cells, anchors, and position.", "ITableAnnotation::RowCount|ITableAnnotation::ColumnCount|ITableAnnotation::Text2|ITableAnnotation::GetAnnotation", "Table geometry/content is addressable."),
                ("Use the specialized BOM/table interface for configuration, numbering, sorting, and feature linkage.", "IBomTableAnnotation|IBomFeature|IHoleTableAnnotation|IRevisionTableAnnotation", "Semantic behavior remains associative."),
            ],
            "verify": "Check specialized type, row/column counts, referenced configuration, item numbers, anchor, sheet bounds, and rebuild.",
            "pitfalls": "ITableAnnotation is implemented by several specialized tables; indices are zero-based in many methods; text overrides can break associations.",
            "chain": "activate view -> insert specialized table -> ITableAnnotation -> specialized interface/feature",
            "terms": "جدول BOM لیست قطعات hole table revision table cut list",
            "symbols": ["IView::InsertBomTable6", "ITableAnnotation::RowCount", "ITableAnnotation::Text2", "IBomTableAnnotation"],
        },
        {
            "slug": "revision-table-cloud", "title": "Manage revision tables and revision clouds", "category": "tables-bom",
            "goal": "Add traceable revision rows/symbols and a revision cloud around changed geometry.",
            "recommended": "Controlled drawing revisions and release workflows.",
            "avoid": "Do not overwrite existing revision cells without reading the current revision feature/table state.",
            "prereq": "Revision policy, target sheet, table template/anchor, and cloud sketch or points.",
            "steps": [
                ("Find an existing revision table on the sheet or insert one with the approved template.", "ISheet::RevisionTable|ISheet::InsertRevisionTable2|IRevisionTableAnnotation", "A revision table annotation is available."),
                ("Add a revision row through the specialized revision-table API and populate only policy-approved cells.", "IRevisionTableAnnotation::AddRevision|ITableAnnotation::Text2", "Revision data is added without destroying table semantics."),
                ("Create/select the cloud boundary and insert a revision cloud.", "IDrawingDoc::InsertRevisionCloud|IRevisionCloud", "Changed geometry is visually marked."),
                ("Link/position the revision symbol/cloud and check layer/style.", "IRevisionCloud::GetAnnotation|IAnnotation::SetPosition2|IAnnotation::Layer", "Revision graphics meet the standard."),
            ],
            "verify": "Confirm revision row/value, symbol, cloud boundary, layer, table anchor, and no duplicate revision IDs.",
            "pitfalls": "Revision table annotation and feature are distinct; cell overrides can interfere with automatic revision behavior.",
            "chain": "find/insert revision table -> AddRevision -> InsertRevisionCloud -> format annotation",
            "terms": "جدول اصلاحات revision table revision cloud ابر بازنگری",
            "symbols": ["ISheet::RevisionTable", "ISheet::InsertRevisionTable2", "IRevisionTableAnnotation::AddRevision", "IDrawingDoc::InsertRevisionCloud"],
        },
        {
            "slug": "title-block-sheet-format", "title": "Read and update title blocks and sheet formats", "category": "sheets",
            "goal": "Populate title block notes through properties while preserving the sheet-format structure.",
            "recommended": "Company templates, drawing metadata, and multi-sheet title blocks.",
            "avoid": "Do not replace resolved note text when a property link should remain dynamic.",
            "prereq": "Approved sheet format and known custom-property ownership (document, configuration, or sheet).",
            "steps": [
                ("Get the active ISheet and title block/title-block table objects.", "IDrawingDoc::GetCurrentSheet|ISheet::TitleBlock|ITitleBlock|ITitleBlockTableAnnotation", "Title block structure is identified."),
                ("Traverse title-block notes and distinguish raw property-link text from resolved display text.", "ITitleBlock::GetNotes|INote::GetText|INote::GetTextAtIndex", "Dynamic links are preserved."),
                ("Update source custom properties through ICustomPropertyManager rather than hard-coding resolved note text.", "IModelDocExtension::CustomPropertyManager|ICustomPropertyManager", "Title block refreshes from authoritative metadata."),
                ("Rebuild and verify each sheet, especially when sheet-specific properties or formats differ.", "IModelDoc2::ForceRebuild3|IDrawingDoc::ActivateSheet", "All sheet title blocks resolve correctly."),
            ],
            "verify": "Compare raw link expression, resolved text, property source, sheet-format path, and all-sheet consistency.",
            "pitfalls": "Title block notes may be sketch notes or table annotations; resolved text should not replace a property expression; sheet-specific context matters.",
            "chain": "GetCurrentSheet -> GetTitleBlock/GetNotes -> CustomPropertyManager -> ForceRebuild3",
            "terms": "کادر نقشه تایتل بلاک title block sheet format custom property مشخصات",
            "symbols": ["ISheet::TitleBlock", "ITitleBlock::GetNotes", "IModelDocExtension::CustomPropertyManager", "IModelDoc2::ForceRebuild3"],
        },
        {
            "slug": "layers-line-styles", "title": "Manage drawing layers, line fonts, and line styles", "category": "layers-style",
            "goal": "Apply deterministic visual organization to annotations, components, and sketch entities.",
            "recommended": "Standards-based drawings and export-compatible layer mapping.",
            "avoid": "Do not assume a layer exists or leave the current layer changed after the macro.",
            "prereq": "Layer naming/color/style policy and target entities selected.",
            "steps": [
                ("Get ILayerMgr from the common document interface and list existing layers.", "IModelDoc2::GetLayerManager|ILayerMgr::GetLayerList", "Existing layer state is known."),
                ("Create missing layers and store the previous current layer.", "ILayerMgr::AddLayer|ILayerMgr::GetCurrentLayer|ILayerMgr::SetCurrentLayer", "Required layer is active without losing prior state."),
                ("Assign annotations/components through their layer or ChangeComponentLayer APIs.", "IAnnotation::Layer|IDrawingDoc::ChangeComponentLayer", "Selected objects are on the intended layer."),
                ("Add/query line styles and restore the previous current layer in a finally/cleanup path.", "IDrawingDoc::AddLineStyle|ILayerMgr::SetCurrentLayer", "Visual state and user context are restored."),
            ],
            "verify": "Enumerate layers, entity layer names, colors/styles, visibility/print behavior, and current-layer restoration.",
            "pitfalls": "Layer manager is drawing-specific; current layer affects later creation; export may map layers differently.",
            "chain": "GetLayerManager -> GetLayerList/AddLayer -> assign Layer -> restore current layer",
            "terms": "لایه خط فونت line style layer color drawing استاندارد",
            "symbols": ["IModelDoc2::GetLayerManager", "ILayerMgr::GetLayerList", "ILayerMgr::AddLayer", "IAnnotation::Layer"],
        },
        {
            "slug": "export-pdf", "title": "Export a drawing to multi-sheet PDF", "category": "export-print",
            "goal": "Create a controlled PDF with explicit sheet selection and error handling.",
            "recommended": "Released drawing packages and batch publishing.",
            "avoid": "Do not use a generic SaveAs without configuring IExportPdfData when sheet scope matters.",
            "prereq": "Rebuilt saved drawing, writable output path, sheet-scope decision, and overwrite approval.",
            "steps": [
                ("Obtain IExportPdfData through ISldWorks.GetExportFileData.", "ISldWorks::GetExportFileData|IExportPdfData", "PDF export options object exists."),
                ("Set export scope and sheets explicitly; configure view-after-save/quality options as required.", "IExportPdfData::SetSheets|IExportPdfData::ViewPdfAfterSaving", "PDF options match the deliverable."),
                ("Call IModelDocExtension.SaveAs3 with PDF path and export data while collecting errors/warnings.", "IModelDocExtension::SaveAs3", "PDF is written or an exact failure is returned."),
                ("Verify file existence/size and expected sheet count; do not treat a Boolean alone as sufficient evidence.", "IExportPdfData::GetWhichSheets", "Export coverage is confirmed."),
            ],
            "verify": "Check SaveAs return, error/warning bitmasks, output file, PDF page count, sheet order, fonts, and line weights.",
            "pitfalls": "SetSheets expects a specific array type; output overwrite is destructive; rebuild errors can propagate into the PDF.",
            "chain": "GetExportFileData -> IExportPdfData.SetSheets -> SaveAs3 -> verify PDF",
            "terms": "خروجی PDF چند شیت export pdf SaveAs چاپ نقشه",
            "symbols": ["ISldWorks::GetExportFileData", "IExportPdfData::SetSheets", "IModelDocExtension::SaveAs3"],
        },
        {
            "slug": "export-dxf-dwg", "title": "Export drawings to DXF or DWG", "category": "export-print",
            "goal": "Publish drawing sheets to DXF/DWG with controlled mapping and restored preferences.",
            "recommended": "2D downstream manufacturing, laser/CAM, and customer exchange.",
            "avoid": "Do not leave global DXF/DWG user preferences changed after a batch export.",
            "prereq": "Rebuilt drawing, output path, layer/font/version mapping policy, and overwrite approval.",
            "steps": [
                ("Snapshot every DXF/DWG user preference the macro will change.", "ISldWorks::GetUserPreferenceIntegerValue|ISldWorks::GetUserPreferenceToggle|ISldWorks::GetUserPreferenceStringValue", "Original global settings are preserved."),
                ("Apply required export version, mapping, endpoint merging, and font options.", "ISldWorks::SetUserPreferenceIntegerValue|ISldWorks::SetUserPreferenceToggle|ISldWorks::SetUserPreferenceStringValue", "Export settings are explicit."),
                ("Save the drawing to .dxf or .dwg through SaveAs3 and collect errors/warnings.", "IModelDocExtension::SaveAs3", "Exchange file is written."),
                ("Restore all captured preferences in cleanup even if export fails.", "ISldWorks::SetUserPreferenceIntegerValue|ISldWorks::SetUserPreferenceToggle|ISldWorks::SetUserPreferenceStringValue", "SOLIDWORKS global state is unchanged."),
            ],
            "verify": "Open/inspect output, layer names, units, text, line types, splines, sheet count, and restored preference values.",
            "pitfalls": "DXF/DWG options are global preferences; SaveAs can overwrite; unit/layer mappings must be checked downstream.",
            "chain": "snapshot preferences -> set export preferences -> SaveAs3 -> restore preferences",
            "terms": "خروجی DXF DWG اتوکد لایه mapping export drawing",
            "symbols": ["ISldWorks::GetUserPreferenceToggle", "ISldWorks::SetUserPreferenceToggle", "IModelDocExtension::SaveAs3"],
        },
        {
            "slug": "print-drawing", "title": "Print selected drawing sheets", "category": "export-print",
            "goal": "Print controlled sheets with explicit page setup, scale, range, copies, and printer behavior.",
            "recommended": "Automated hard-copy issue sets and shop-floor packets.",
            "avoid": "Do not rely on interactive printer defaults in unattended automation.",
            "prereq": "Known printer, sheet selection/range, page size/orientation, and scaling policy.",
            "steps": [
                ("Get sheet/page setup and inspect paper size, orientation, scale-to-fit, and margins.", "ISheet::PageSetup|IPageSetup", "Page settings are known."),
                ("Use IPrintSpecification for controlled sheet ranges/copies where supported.", "IModelDocExtension::PrintOut4|IPrintSpecification", "Print job is fully specified."),
                ("Submit the print and capture return/error behavior; avoid UI dialogs in silent batches.", "IModelDocExtension::PrintOut4", "A print job is accepted or an actionable failure is logged."),
            ],
            "verify": "Confirm printer/job, sheet range/order, paper size, orientation, scale, line weight, and output completeness.",
            "pitfalls": "Printer capabilities and driver defaults are external state; page setup can differ per sheet.",
            "chain": "GetPageSetup -> create/configure PrintSpecification -> PrintOut4",
            "terms": "پرینت چاپ نقشه printer page setup sheet range copies",
            "symbols": ["ISheet::PageSetup", "IModelDocExtension::PrintOut4", "IPrintSpecification"],
        },
        {
            "slug": "traverse-drawing-content", "title": "Traverse sheets, views, dimensions, annotations, and components", "category": "inspection",
            "goal": "Build a complete in-memory inventory of a drawing before editing or validating it.",
            "recommended": "Audits, search/replace, layout checks, repair, and export manifests.",
            "avoid": "Do not rely solely on the feature tree; many drawing objects have dedicated traversal APIs.",
            "prereq": "Open drawing; traversal code handles Nothing/empty Variant arrays.",
            "steps": [
                ("Enumerate sheet names and activate each sheet in turn.", "IDrawingDoc::GetSheetNames|IDrawingDoc::ActivateSheet", "Each sheet becomes the current context."),
                ("Traverse GetFirstView/GetNextView and classify the first sheet view separately.", "IDrawingDoc::GetFirstView|IView::GetNextView|IView::Type", "All views are inventoried."),
                ("Within each view traverse display dimensions, annotations, tables, and drawing components using their dedicated getters.", "IView::GetFirstDisplayDimension6|IDisplayDimension::GetNext5|IView::GetAnnotations|IView::GetTableAnnotations|IView::RootDrawingComponent2", "All visible semantic objects are captured."),
                ("Record persistent references or stable composite keys before any mutation.", "IModelDocExtension::GetPersistReference3|IView::GetName2", "Objects can be re-identified after rebuild."),
            ],
            "verify": "Compare counts against UI/tree, detect duplicates, Nothing arrays, hidden/suppressed items, and all-sheet totals.",
            "pitfalls": "Sheet view comes first; some arrays are Variants; hidden or detached views can behave differently; rebuild can invalidate transient COM objects.",
            "chain": "GetSheetNames -> ActivateSheet -> GetFirstView/GetNextView -> per-view traversals -> persistent refs",
            "terms": "پیمایش نقشه شیت نما ابعاد annotations traverse inventory audit",
            "symbols": ["IDrawingDoc::GetSheetNames", "IDrawingDoc::GetFirstView", "IView::GetAnnotations", "IModelDocExtension::GetPersistReference3"],
        },
        {
            "slug": "selection-strategy", "title": "Use deterministic drawing selection", "category": "selection",
            "goal": "Make selection-dependent drawing API calls reproducible across sessions and rebuilds.",
            "recommended": "Dimensions, section/detail views, callouts, tables, and entity-specific annotations.",
            "avoid": "Avoid coordinate-only SelectByID2 when a direct IEntity or persistent reference is available.",
            "prereq": "Known target sheet/view/entity identity and expected selection marks/order.",
            "steps": [
                ("Activate the owning sheet and view; clear the existing selection list.", "IDrawingDoc::ActivateSheet|IDrawingDoc::ActivateView|IModelDoc2::ClearSelection2", "Selection context is clean."),
                ("Prefer IEntity.Select4 with ISelectData/mark when an entity object is available.", "IEntity::Select4|ISelectionMgr::CreateSelectData", "Selection uses object identity and correct mark."),
                ("Use SelectByID2 only with exact name/type and validated coordinates/context.", "IModelDocExtension::SelectByID2", "Fallback selection is explicit."),
                ("Verify count, object type, and selected object before invoking the consuming method.", "ISelectionMgr::GetSelectedObjectCount2|ISelectionMgr::GetSelectedObjectType3|ISelectionMgr::GetSelectedObject6", "Preconditions are proven."),
                ("Store/recover persistent references for selections that must survive rebuild or reopen.", "IModelDocExtension::GetPersistReference3", "Long-lived identity is available."),
            ],
            "verify": "Assert selection count, order, mark, selected-object type, owning view, and consuming method result.",
            "pitfalls": "Many APIs encode selection contracts only in Remarks; marks and order matter; stale COM entity pointers can fail after rebuild.",
            "chain": "Activate context -> ClearSelection2 -> Select4/SelectByID2 -> inspect SelectionMgr -> call consumer",
            "terms": "انتخاب deterministic selection mark SelectByID2 Select4 persistent reference",
            "symbols": ["IEntity::Select4", "ISelectionMgr::CreateSelectData", "IModelDocExtension::SelectByID2", "ISelectionMgr::GetSelectedObject6"],
        },
        {
            "slug": "validate-save-close", "title": "Rebuild, validate, save, and close a drawing", "category": "document-lifecycle",
            "goal": "Finish automation with evidence that the drawing rebuilt and persisted correctly.",
            "recommended": "Every mutating drawing script or macro.",
            "avoid": "Never report success based only on a screenshot or a Boolean without checking errors/warnings and saved state.",
            "prereq": "All intended edits complete and output overwrite policy approved.",
            "steps": [
                ("Force a full rebuild and inspect rebuild errors before saving.", "IModelDoc2::ForceRebuild3|IModelDoc2::EditRebuild3", "Model/drawing dependency errors are surfaced."),
                ("Traverse sheets/views/annotations for expected counts, dangling items, and layout bounds.", "IDrawingDoc::GetSheetNames|IDrawingDoc::GetFirstView|IView::GetOutline", "Acceptance criteria are measured."),
                ("Save through Save3 or SaveAs3 and capture both error and warning bitmasks.", "IModelDoc2::Save3|IModelDocExtension::SaveAs3", "Persistence result is explicit."),
                ("Confirm path/title and file existence; close with ISldWorks.CloseDoc only after successful validation/save.", "IModelDoc2::GetPathName|ISldWorks::CloseDoc", "The completed document is closed cleanly."),
            ],
            "verify": "Rebuild result, expected object counts, no dangling annotations, sheet bounds, save errors/warnings, output existence, and reopen test.",
            "pitfalls": "CloseDoc uses document title/name semantics; save warning bitmasks are not necessarily fatal but must be reported; rebuild can invalidate object references.",
            "chain": "ForceRebuild3 -> inspect drawing -> Save3/SaveAs3 -> verify file -> CloseDoc",
            "terms": "اعتبارسنجی rebuild save close ذخیره بستن نقشه خطا warning",
            "symbols": ["IModelDoc2::ForceRebuild3", "IModelDoc2::Save3", "IModelDocExtension::SaveAs3", "ISldWorks::CloseDoc"],
        },
        {
            "slug": "batch-drawing-generation", "title": "Generate drawings in a batch", "category": "automation-architecture",
            "goal": "Process many models without leaking document state, preferences, selections, or COM references.",
            "recommended": "Standardized production drawing creation and publishing.",
            "avoid": "Do not keep all models/drawings open or assume one template/layout works for every bounding box.",
            "prereq": "Input manifest, approved templates, naming/overwrite policy, error log, and acceptance criteria.",
            "steps": [
                ("For each input, normalize path and open model silently with captured errors.", "ISldWorks::OpenDoc7|ISldWorks::OpenDoc6", "A valid source model is available."),
                ("Create a drawing from the approved template and insert/lay out required views.", "ISldWorks::NewDocument|IDrawingDoc::CreateDrawViewFromModelView3|IView::GetOutline", "Drawing geometry is created and fitted."),
                ("Add required dimensions/annotations/tables from a declared recipe rather than recorded cursor positions.", "IDrawingDoc::InsertModelAnnotations3|IView::InsertBomTable6", "Semantic content is reproducible."),
                ("Rebuild, validate, save/export, log measurements/errors, and close drawing then source model.", "IModelDoc2::ForceRebuild3|IModelDocExtension::SaveAs3|ISldWorks::CloseDoc", "Each item ends in a known state."),
                ("Restore global preferences in a final cleanup path and release COM references between items.", "ISldWorks::SetUserPreferenceToggle", "The next item is isolated from prior state."),
            ],
            "verify": "Per-file result manifest: source/output paths, view/sheet counts, scale/layout metrics, rebuild state, save/export codes, and elapsed time.",
            "pitfalls": "Global preferences leak across items; hidden dialogs block unattended runs; COM references and open documents accumulate; layout must respond to measured outlines.",
            "chain": "Open model -> New drawing -> create/layout -> annotate -> validate -> save/export -> close -> cleanup",
            "terms": "تولید دسته ای نقشه batch drawing automation چند فایل template export log",
            "symbols": ["ISldWorks::OpenDoc7", "ISldWorks::NewDocument", "IView::GetOutline", "IModelDocExtension::SaveAs3", "ISldWorks::CloseDoc"],
        },
        {
            "slug": "document-manager-inspection", "title": "Inspect drawings without opening SOLIDWORKS", "category": "document-manager",
            "goal": "Read sheets, views, previews, references, and properties through SOLIDWORKS Document Manager.",
            "recommended": "Fast vault scans, metadata audits, preview generation, and dependency inventories.",
            "avoid": "Document Manager is not a replacement for geometry creation, drawing rebuild, or most authoring operations.",
            "prereq": "Valid Document Manager license key and compatible API version.",
            "steps": [
                ("Create the Document Manager application and open the SLDDrw with the correct document type/read-only intent.", "ISwDMApplication::GetDocument", "A Document Manager drawing object is available."),
                ("Read sheet count/names/properties and obtain ISwDMSheet objects.", "ISwDMDocument4::GetSheetCount|ISwDMDocument6::GetSheetNames|ISwDMDocument10::GetSheets|ISwDMDocument13::GetSheetProperties", "Sheet metadata is inventoried."),
                ("Traverse ISwDMSheet.GetViews / ISwDMDocument10.GetViews and record referenced documents/configurations.", "ISwDMSheet4::GetViews|ISwDMView::ReferencedDocument|ISwDMView::ReferencedConfiguration", "View dependencies are known."),
                ("Optionally read preview PNG/bitmap data and close the document.", "ISwDMSheet2::GetPreviewPNGBitmapBytes|ISwDMDocument::CloseDoc", "Preview/metadata scan completes without loading SOLIDWORKS UI."),
            ],
            "verify": "Compare sheet/view/reference counts to a known drawing and report API/version limitations.",
            "pitfalls": "License key is mandatory; versioned interfaces expose newer methods; Document Manager data can be stale until the drawing was last rebuilt/saved in SOLIDWORKS.",
            "chain": "GetDocument -> GetSheetNames/GetSheets -> GetViews -> references/previews -> CloseDoc",
            "terms": "بررسی آفلاین نقشه Document Manager شیت نما preview reference بدون باز کردن سالیدورکس",
            "symbols": ["ISwDMDocument4::GetSheetCount", "ISwDMDocument6::GetSheetNames", "ISwDMDocument10::GetSheets", "ISwDMSheet4::GetViews"],
        },
        {
            "slug": "macro-chaining", "title": "Run one SOLIDWORKS macro from another", "category": "macro-runtime",
            "goal": "Invoke a known procedure in another macro project and capture run errors.",
            "recommended": "Small modular macro suites and controlled entry-point reuse.",
            "avoid": "Do not use macro chaining as a substitute for a maintainable add-in architecture at large scale.",
            "prereq": "Existing macro project path, module name, public Sub with no arguments, and trusted macro policy.",
            "steps": [
                ("Resolve the absolute macro path and exact module/procedure entry point.", "ISldWorks::GetCurrentMacroPathName", "Target entry point is explicit."),
                ("Call RunMacro2 and capture the Boolean plus swRunMacroError_e status.", "ISldWorks::RunMacro2|swRunMacroError_e", "Success or exact macro-run failure is known."),
                ("Keep document activation and shared global state explicit between caller and callee.", "ISldWorks::ActivateDoc3", "The callee runs in the intended context."),
            ],
            "verify": "Check return/error enum, expected document/result change, and that the correct procedure ran.",
            "pitfalls": "Entry procedure cannot take arguments; module/procedure names are case/context sensitive in practice; obsolete RunMacro should be avoided.",
            "chain": "resolve path -> RunMacro2 -> inspect swRunMacroError_e/result",
            "terms": "اجرای ماکرو از ماکرو RunMacro2 procedure module swp",
            "symbols": ["ISldWorks::RunMacro2", "swRunMacroError_e", "ISldWorks::GetCurrentMacroPathName"],
        },
        {
            "slug": "units-coordinate-system", "title": "Handle drawing units and coordinates correctly", "category": "fundamentals",
            "goal": "Prevent geometry/layout errors caused by confusing display units with API system units.",
            "recommended": "Every script that creates, positions, scales, or measures drawing objects.",
            "avoid": "Never pass millimeter values directly to APIs documented in meters.",
            "prereq": "Input values carry explicit units and conversion helpers are centralized.",
            "steps": [
                ("Treat drawing sheet positions, view centers, sketch coordinates, and many dimensions as meters unless the member page states otherwise.", "IView::Position|IDrawingDoc::CreateDrawViewFromModelView3", "All geometry calls receive SI values."),
                ("Read IUserUnit/display settings only for formatting and user-facing conversion.", "IModelDoc2::GetUserUnit|IUserUnit", "Display and system units are separated."),
                ("Represent sheet scale as numerator/denominator or ScaleDecimal according to the exact property contract.", "ISheet::GetProperties2|IView::ScaleDecimal", "Scale math is unambiguous."),
                ("Log values with unit suffixes and convert mm->m at API boundaries.", "IView::GetOutline|IView::Position", "Diagnostics can be audited."),
            ],
            "verify": "Known 100 mm test distance reads/writes as 0.1 m internally; scale and sheet dimensions round-trip correctly.",
            "pitfalls": "Display units do not change API SI contracts; angles are often radians; array ordering is member-specific.",
            "chain": "explicit input units -> boundary conversion -> API call -> measured round-trip",
            "terms": "واحد متر میلیمتر مختصات رادیان unit conversion mm m scale drawing",
            "symbols": ["IView::Position", "IView::GetOutline", "IView::ScaleDecimal", "IUserUnit"],
        },
        {
            "slug": "drawing-events", "title": "React to drawing document events", "category": "events-addins",
            "goal": "Observe view, sheet, save, selection, table, and rebuild events in an add-in without recursive side effects.",
            "recommended": ".NET/COM add-ins that maintain metadata, validation, or UI state.",
            "avoid": "Do not perform heavy or recursively triggering edits directly inside notification handlers.",
            "prereq": "A registered add-in, event delegate lifetime management, and reentrancy guards.",
            "steps": [
                ("Subscribe to the specific DDrawingDocEvents delegates required by the feature.", "DDrawingDocEvents_ViewNewNotify2EventHandler|DDrawingDocEvents_ActivateSheetPostNotifyEventHandler|DDrawingDocEvents_FileSavePostNotifyEventHandler", "Only required events are handled."),
                ("In handlers, capture lightweight context and defer heavy work outside the notification when possible.", "DDrawingDocEvents_RegenPostNotifyEventHandler", "SOLIDWORKS notification flow remains stable."),
                ("Use reentrancy guards before any API call that can trigger the same event.", "DDrawingDocEvents_ModifyNotifyEventHandler", "Recursive event loops are prevented."),
                ("Unsubscribe delegates when documents close or the add-in disconnects.", "DDrawingDocEvents_DestroyNotify2EventHandler", "No leaked COM/event references remain."),
            ],
            "verify": "Log one handler call per user action, test close/reopen, confirm no recursion, and confirm add-in unload releases delegates.",
            "pitfalls": "Event handlers run in SOLIDWORKS context; return codes can affect behavior; stale delegate references leak documents/add-ins.",
            "chain": "subscribe delegates -> guard/defer handler work -> unsubscribe on destroy/disconnect",
            "terms": "رویداد نقشه event add-in save view sheet rebuild notification",
            "symbols": ["DDrawingDocEvents_ViewNewNotify2EventHandler", "DDrawingDocEvents_FileSavePostNotifyEventHandler", "DDrawingDocEvents_DestroyNotify2EventHandler"],
        },
    ]


def workflow_symbol_status(conn: sqlite3.Connection, symbol: str) -> tuple[str, str, str]:
    """Return (status, url, replacement) for a declared workflow symbol."""
    symbol = symbol.strip()
    if not symbol:
        return "empty", "", ""
    if "::" in symbol:
        type_name, member_name = symbol.split("::", 1)
        row = conn.execute(
            "SELECT m.official_url,m.is_obsolete,m.superseded_by FROM api_members m "
            "JOIN api_types t ON t.id=m.type_id WHERE t.name=? AND m.name=?",
            (type_name, member_name),
        ).fetchone()
        if not row:
            return "missing", "", ""
        return ("obsolete" if row[1] else "ok"), row[0], row[2] or ""
    row = conn.execute("SELECT official_url FROM api_types WHERE name=?", (symbol,)).fetchone()
    if row:
        return "ok", row[0], ""
    row = conn.execute("SELECT official_url FROM api_enums WHERE name=?", (symbol,)).fetchone()
    if row:
        return "ok", row[0], ""
    return "missing", "", ""


def ingest_workflows(conn: sqlite3.Connection) -> tuple[int, list[str], list[str]]:
    unresolved: list[str] = []
    obsolete_symbols: list[str] = []
    for wf in curated_workflows():
        urls = []
        declared = list(wf["symbols"])
        for _, symbols, _ in wf["steps"]:
            declared.extend(s.strip() for s in symbols.split("|") if s.strip())
        for symbol in dict.fromkeys(declared):
            status, url, replacement = workflow_symbol_status(conn, symbol)
            if url and url not in urls:
                urls.append(url)
            if status == "missing":
                unresolved.append(f"{wf['slug']}: {symbol}")
            elif status == "obsolete":
                obsolete_symbols.append(f"{wf['slug']}: {symbol} -> {replacement or 'no replacement documented'}")
        cur = conn.execute(
            "INSERT INTO workflows(slug,title,category,goal,recommended_when,avoid_when,prerequisites,verification,pitfalls,"
            "primary_api_chain,search_terms,source_urls_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                wf["slug"], wf["title"], wf["category"], wf["goal"], wf["recommended"], wf["avoid"],
                wf["prereq"], wf["verify"], wf["pitfalls"], wf["chain"], wf["terms"],
                json.dumps(urls, ensure_ascii=False),
            ),
        )
        workflow_id = int(cur.lastrowid)
        for seq, (instruction, symbols, expected) in enumerate(wf["steps"], 1):
            conn.execute(
                "INSERT INTO workflow_steps(workflow_id,sequence,instruction,api_symbols,expected_result) VALUES(?,?,?,?,?)",
                (workflow_id, seq, instruction, symbols, expected),
            )
    return len(curated_workflows()), sorted(set(unresolved)), sorted(set(obsolete_symbols))


def ingest_aliases(conn: sqlite3.Connection) -> int:
    aliases = {
        "drawing": ["نقشه", "نقشه کشی", "drafting", "slddrw"],
        "sheet": ["شیت", "صفحه نقشه", "برگه"],
        "drawing view": ["نما", "نمای نقشه", "view"],
        "section view": ["نمای برش", "مقطع"],
        "detail view": ["نمای جزئیات", "بزرگنمایی"],
        "auxiliary view": ["نمای کمکی"],
        "projected view": ["نمای پروجکت", "نمای تصویر شده"],
        "flat pattern": ["نمای بازشده", "گسترش ورق", "ورقکاری"],
        "dimension": ["بعد", "ابعاد", "اندازه گذاری"],
        "tolerance": ["تلرانس", "رواداری"],
        "annotation": ["نشانه گذاری", "توضیحات نقشه"],
        "note": ["یادداشت", "متن نقشه"],
        "balloon": ["بالون", "شماره آیتم"],
        "bill of materials": ["BOM", "لیست قطعات", "جدول قطعات"],
        "title block": ["کادر نقشه", "جدول مشخصات"],
        "revision": ["بازنگری", "اصلاحات", "ریویژن"],
        "layer": ["لایه"],
        "selection": ["انتخاب", "سلکشن"],
        "macro": ["ماکرو", "اسکریپت"],
        "export": ["خروجی", "تبدیل", "ذخیره به"],
        "print": ["چاپ", "پرینت"],
        "Document Manager": ["مدیریت اسناد", "بررسی آفلاین"],
    }
    count = 0
    for canonical, values in aliases.items():
        for alias in values:
            lang = "fa" if re.search(r"[\u0600-\u06ff]", alias) else "en"
            conn.execute(
                "INSERT OR IGNORE INTO aliases(canonical,alias,language,category) VALUES(?,?,?,?)",
                (canonical, alias, lang, "drawing-search"),
            )
            count += 1
    return count


def build_search_index(conn: sqlite3.Connection) -> int:
    conn.execute("DELETE FROM search_fts")
    rows = []
    for row in conn.execute(
        "SELECT t.id,t.name,t.summary,t.remarks,t.search_terms,t.official_url FROM api_types t"
    ):
        rows.append(("api_type", row[0], row[1], clean_text((row[2] or "") + " " + (row[3] or "")), row[4] or "", row[5]))
    for row in conn.execute(
        "SELECT m.id,t.name||'::'||m.name,m.summary,m.remarks,m.signature_vba,m.signature_csharp,m.selection_requirements,"
        "m.unit_notes,m.drawing_tasks,m.enum_references,m.official_url FROM api_members m JOIN api_types t ON t.id=m.type_id"
    ):
        body = "\n".join(clean_text(x or "") for x in row[2:8] if x)
        tags = " ".join(clean_text(x or "") for x in row[8:10] if x)
        rows.append(("api_member", row[0], row[1], body, tags, row[10]))
    for row in conn.execute("SELECT id,name,summary,remarks,drawing_relevance,official_url FROM api_enums"):
        values = " ".join(
            f"{v[0]}={v[1]} {v[2]}" for v in conn.execute(
                "SELECT name,numeric_value,description FROM api_enum_values WHERE enum_id=?", (row[0],)
            )
        )
        rows.append(("api_enum", row[0], row[1], clean_text((row[2] or "") + " " + (row[3] or "") + " " + values), row[4] or "", row[5]))
    for row in conn.execute(
        "SELECT id,title,objective,prerequisites,postconditions,api_types_json,api_calls_json,relevance_reason,search_terms,official_url FROM examples"
    ):
        rows.append(("example", row[0], row[1], "\n".join(clean_text(x or "") for x in row[2:8]), row[8] or "", row[9]))
    for row in conn.execute("SELECT id,title,summary,section_outline,search_terms,official_url FROM guide_topics"):
        rows.append(("guide_topic", row[0], row[1], clean_text((row[2] or "") + " " + (row[3] or "")), row[4] or "", row[5]))
    for row in conn.execute(
        "SELECT id,title,goal,recommended_when,avoid_when,prerequisites,verification,pitfalls,primary_api_chain,search_terms,source_urls_json FROM workflows"
    ):
        steps = " ".join(
            " ".join(clean_text(x or "") for x in step)
            for step in conn.execute(
                "SELECT instruction,api_symbols,expected_result FROM workflow_steps WHERE workflow_id=? ORDER BY sequence",
                (row[0],),
            )
        )
        body = "\n".join(clean_text(x or "") for x in row[2:9]) + "\n" + steps
        source_urls = json.loads(row[10]) if row[10] else []
        rows.append(("workflow", row[0], row[1], body, row[9] or "", source_urls[0] if source_urls else ""))
    for canonical, alias, language, category in conn.execute("SELECT canonical,alias,language,category FROM aliases"):
        rows.append(("alias", 0, f"{canonical}: {alias}", canonical, f"{language} {category}", ""))
    conn.executemany(
        "INSERT INTO search_fts(entity_kind,entity_id,title,body,tags,source_url) VALUES(?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def populate_coverage(
    conn: sqlite3.Connection,
    main_result: dict[str, Any],
    dm_result: dict[str, Any],
    guide_count: int,
    example_count: int,
    unresolved_workflow_symbols: list[str],
    obsolete_workflow_symbols: list[str],
) -> None:
    now = utc_now()
    entries = [
        (
            "SOLIDWORKS drawing interfaces and members", "complete-for-declared-scope",
            conn.execute("SELECT COUNT(*) FROM api_members m JOIN api_types t ON t.id=m.type_id WHERE t.library='sldworks'").fetchone()[0],
            "All member pages for drawing-specific interfaces; curated drawing-relevant members for broad common interfaces.",
            (
                "The database is a drawing-automation knowledge base, not a verbatim mirror of every modeling API. "
                + (
                    "Declared compatibility names without a standalone 2026 type page: "
                    + ", ".join(main_result.get("missing_declared_types", []))
                    if main_result.get("missing_declared_types") else "All declared drawing/common type pages were found."
                )
            ),
        ),
        (
            "VBA signatures", "best-available-official",
            conn.execute("SELECT COUNT(*) FROM api_members WHERE signature_vba<>''").fetchone()[0],
            "VBA signatures joined from the installed official sldworksapivb6.chm by type/member identity.",
            "Some .NET-only delegates and newer topics do not expose a VBA signature.",
        ),
        (
            "Drawing-related enumerations and command IDs", "complete-for-references-and-keywords",
            conn.execute("SELECT COUNT(*) FROM api_enum_values").fetchone()[0],
            "Every enum referenced by included members, every drawing-keyword enum, and all swCommands_e values.",
            "Non-drawing enumerations not referenced by the selected APIs are intentionally excluded.",
        ),
        (
            "Official examples", "complete-for-relevance-rule", example_count,
            "Examples linked by included member pages or selected by drawing terms/types; source code is not mirrored, but objectives, pre/postconditions, call inventories, and links are indexed.",
            "Open the exact official source URL for full example code.",
        ),
        (
            "API programming guide", "complete-summary-index", guide_count,
            "Every HTML topic from the installed official 2026 programming guide is indexed by title, category, outline, and compact summary.",
            "The database is an index/synthesis, not a full-text redistribution of the guide.",
        ),
        (
            "Document Manager drawing inspection", "complete-for-declared-scope",
            conn.execute("SELECT COUNT(*) FROM api_members m JOIN api_types t ON t.id=m.type_id WHERE t.library='swdocumentmgr'").fetchone()[0],
            "All members of sheet/view/table interfaces plus drawing/document/preview/reference members of versioned document/application interfaces.",
            "Document Manager mainly inspects metadata and previews; it does not author/rebuild normal drawing geometry.",
        ),
        (
            "Task workflows", "validated" if not unresolved_workflow_symbols and not obsolete_workflow_symbols else "validation-failed",
            conn.execute("SELECT COUNT(*) FROM workflows").fetchone()[0],
            "Original task-oriented workflows synthesized from the included official API records.",
            (
                (("Unresolved symbols: " + "; ".join(unresolved_workflow_symbols) + ". ") if unresolved_workflow_symbols else "")
                + (("Obsolete symbols: " + "; ".join(obsolete_workflow_symbols)) if obsolete_workflow_symbols else "")
            ) or "All declared step and primary symbols resolve to current, non-obsolete API records.",
        ),
    ]
    conn.executemany(
        "INSERT INTO coverage(area,status,records,scope_rule,limitations,checked_at) VALUES(?,?,?,?,?,?)",
        [(a, s, int(r), rule, lim, now) for a, s, r, rule, lim in entries],
    )


def validate_database(conn: sqlite3.Connection) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    checks["integrity_check"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
    checks["foreign_key_violations"] = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    checks["counts"] = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "source_sets", "source_pages", "api_types", "api_members", "api_parameters", "api_enums",
            "api_enum_values", "examples", "example_calls", "guide_topics", "workflows", "workflow_steps",
            "relationships", "aliases", "coverage", "search_fts",
        )
    }
    required = [
        ("IDrawingDoc", "CreateDrawViewFromModelView3"),
        ("IDrawingDoc", "CreateSectionViewAt5"),
        ("IDrawingDoc", "InsertModelAnnotations3"),
        ("IView", "GetOutline"),
        ("ITableAnnotation", "Text"),
        ("IModelDocExtension", "SaveAs3"),
    ]
    missing = []
    for type_name, member_name in required:
        exists = conn.execute(
            "SELECT 1 FROM api_members m JOIN api_types t ON t.id=m.type_id WHERE t.name=? AND m.name=?",
            (type_name, member_name),
        ).fetchone()
        if not exists:
            missing.append(f"{type_name}::{member_name}")
    checks["missing_required_symbols"] = missing
    query_tests = {}
    for query in ("section view", "CreateDrawViewFromModelView3", "BOM table", "خروجی PDF", "نمای برش"):
        query_tests[query] = conn.execute(
            "SELECT COUNT(*) FROM search_fts WHERE search_fts MATCH ?", (f'"{query}"',)
        ).fetchone()[0]
    checks["query_tests"] = query_tests
    workflow_row = conn.execute("SELECT status,limitations FROM coverage WHERE area='Task workflows'").fetchone()
    checks["workflow_status"] = workflow_row[0] if workflow_row else "missing"
    checks["workflow_notes"] = workflow_row[1] if workflow_row else "coverage row missing"
    checks["pass"] = (
        checks["integrity_check"] == "ok"
        and checks["foreign_key_violations"] == 0
        and not missing
        and all(v > 0 for v in query_tests.values())
        and checks["workflow_status"] == "validated"
    )
    return checks


def write_manifest(path: Path, db_path: Path, checks: dict[str, Any]) -> None:
    payload = {
        "database": str(db_path),
        "database_sha256": sha256_file(db_path),
        "database_bytes": db_path.stat().st_size,
        "built_at": utc_now(),
        "product_version": "SOLIDWORKS 2026",
        "language": "English source documentation; English/Persian search aliases and workflow tags",
        "scope": "Drawing creation and automation with macros/scripts, plus dependencies, enums, examples, commands, and Document Manager inspection.",
        "validation": checks,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_readme(path: Path, db_path: Path, checks: dict[str, Any]) -> None:
    counts = checks["counts"]
    text = f"""# SOLIDWORKS Drawing API Database 2026

This is a normalized, source-traceable SQLite knowledge base for drawing creation
and automation with SOLIDWORKS macros, scripts, COM add-ins, and Document Manager.

- Database: `{db_path.name}`
- SHA-256: `{sha256_file(db_path)}`
- Validation: `{checks['pass']}` (`PRAGMA integrity_check = {checks['integrity_check']}`)

## Contents

- {counts['api_types']:,} API types/interfaces/event delegates
- {counts['api_members']:,} methods/properties with VBA/.NET/C# signatures, parameters, returns, remarks, selection/unit notes, availability, and obsolete replacements
- {counts['api_parameters']:,} normalized parameters
- {counts['api_enums']:,} relevant enumerations and the full command-ID enumeration
- {counts['api_enum_values']:,} enumeration/command values
- {counts['examples']:,} official relevant examples indexed by objective, preconditions, postconditions, API calls, language, and exact source URL
- {counts['guide_topics']:,} official Programming Guide topics
- {counts['workflows']:,} task workflows with {counts['workflow_steps']:,} ordered steps
- {counts['search_fts']:,} full-text search documents (English and Persian aliases/tags)

The database stores compact structured facts and original workflow synthesis, not a
verbatim offline mirror. Open `official_url` for the complete official page/example.

## Quick search

```sql
SELECT entity_kind, title, snippet(search_fts, 3, '[', ']', ' … ', 24) AS hit, source_url
FROM search_fts
WHERE search_fts MATCH '"section view"'
ORDER BY rank
LIMIT 20;
```

Persian aliases are indexed too:

```sql
SELECT entity_kind, title, snippet(search_fts, 3, '[', ']', ' … ', 24) AS hit
FROM search_fts
WHERE search_fts MATCH '"نمای برش" OR section'
ORDER BY rank
LIMIT 20;
```

Find the recommended workflow first, then resolve its exact calls:

```sql
SELECT * FROM v_workflow_api_map WHERE slug='create-section-view';

SELECT * FROM v_api_member_details
WHERE api_type='IDrawingDoc' AND api_member='CreateSectionViewAt5';
```

Find non-obsolete methods for a task:

```sql
SELECT api_type, api_member, summary, signature_vba, selection_requirements,
       unit_notes, availability, official_url
FROM v_api_member_details
WHERE is_obsolete=0 AND drawing_tasks LIKE '%views%'
ORDER BY api_type, api_member;
```

## Important scope rules

- Every member of drawing-specific interfaces is included.
- Broad interfaces such as `ISldWorks`, `IModelDoc2`, and `IModelDocExtension` are
  limited to members needed by drawing workflows.
- Every enum referenced by those members is included, plus drawing-keyword enums
  and all `swCommands_e` values.
- Full official example source is accessed through its `official_url`; the database
  keeps searchable behavior, requirements, effects, and API-call inventories.
- API geometry/placement values are often SI (meters/radians). Always use the exact
  member parameter descriptions and `unit_notes`.
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_database(args: argparse.Namespace) -> dict[str, Any]:
    args.db.parent.mkdir(parents=True, exist_ok=True)
    if args.db.exists():
        # Build to a sibling temporary file and atomically replace only after
        # validation, so an interrupted run never destroys a usable database.
        temp_db = args.db.with_suffix(args.db.suffix + ".building")
    else:
        temp_db = args.db.with_suffix(args.db.suffix + ".building")
    if temp_db.exists():
        temp_db.unlink()
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA_SQL)
        conn.executemany(
            "INSERT INTO metadata(key,value) VALUES(?,?)",
            [
                ("database_name", "SOLIDWORKS Drawing API Database"),
                ("schema_version", "1.0.0"),
                ("product_version", "2026"),
                ("source_language", "English"),
                ("built_at", utc_now()),
                ("source_policy", "Official installed CHM documentation mapped to official Web Help URLs"),
                ("content_policy", "Structured facts, compact summaries, exact signatures, and original workflow synthesis; no verbatim offline mirror"),
                ("coordinate_policy", "Preserve exact member unit notes; SOLIDWORKS API commonly uses meters/radians"),
            ],
        )
        print("[1/9] Registering official source sets", flush=True)
        source_ids = create_source_sets(conn)
        print("[2/9] Parsing drawing and common SOLIDWORKS API types/members", flush=True)
        main_result = ingest_api_library(conn, source_ids, "sldworksapi")
        print(
            f"      types={len(main_result['type_ids'])} enum_refs={len(main_result['enum_refs'])} "
            f"missing_declared_types={len(main_result['missing_declared_types'])}",
            flush=True,
        )
        print("[3/9] Parsing Document Manager drawing-inspection API", flush=True)
        dm_result = ingest_api_library(conn, source_ids, "swdocmgrapi")
        print(
            f"      types={len(dm_result['type_ids'])} missing_declared_types={len(dm_result['missing_declared_types'])}",
            flush=True,
        )
        enum_counts = main_result["enum_refs"] + dm_result["enum_refs"]
        print("[4/9] Parsing referenced/drawing enums and command IDs", flush=True)
        enum_ids = ingest_enums(conn, source_ids, enum_counts)
        print(f"      enums={len(enum_ids)}", flush=True)
        print("[5/9] Indexing the complete official Programming Guide", flush=True)
        guide_count = ingest_programming_guide(conn, source_ids)
        print(f"      guide_topics={guide_count}", flush=True)
        print("[6/9] Mining drawing-relevant official examples", flush=True)
        linked = set(main_result["example_links"]) | set(dm_result["example_links"])
        example_count = ingest_examples(conn, source_ids, linked)
        print(f"      examples={example_count}", flush=True)
        print("[7/9] Adding task workflows and bilingual aliases", flush=True)
        workflow_count, unresolved, obsolete_workflow_symbols = ingest_workflows(conn)
        alias_count = ingest_aliases(conn)
        print(
            f"      workflows={workflow_count} aliases={alias_count} unresolved_symbols={len(unresolved)} "
            f"obsolete_symbols={len(obsolete_workflow_symbols)}",
            flush=True,
        )
        print("[8/9] Building FTS5 index and coverage report", flush=True)
        fts_count = build_search_index(conn)
        populate_coverage(
            conn, main_result, dm_result, guide_count, example_count, unresolved, obsolete_workflow_symbols
        )
        for code, sid in source_ids.items():
            selected = conn.execute("SELECT COUNT(*) FROM source_pages WHERE source_set_id=?", (sid,)).fetchone()[0]
            conn.execute("UPDATE source_sets SET selected_pages=? WHERE id=?", (selected, sid))
        conn.commit()
        print(f"      search_documents={fts_count}", flush=True)
        print("[9/9] Validating database", flush=True)
        checks = validate_database(conn)
        conn.commit()
        if not checks["pass"]:
            raise RuntimeError("Database validation failed: " + json.dumps(checks, ensure_ascii=False))
        conn.execute("PRAGMA optimize")
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()

    os.replace(temp_db, args.db)
    # Re-open final file so validation reflects the delivered artifact.
    final_conn = sqlite3.connect(args.db)
    try:
        checks = validate_database(final_conn)
    finally:
        final_conn.close()
    write_manifest(args.manifest, args.db, checks)
    write_readme(args.readme, args.db, checks)
    return checks


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect-url", help="Fetch one official help page and print parser diagnostics")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--readme", type=Path, default=DEFAULT_README)
    parser.add_argument("--delay", type=float, default=0.12, help="Polite delay between HTTP requests")
    parser.add_argument("--limit", type=int, default=0, help="Development-only page limit; 0 means unlimited")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    if args.inspect_url:
        return inspect(args.inspect_url)
    checks = build_database(args)
    print(json.dumps({
        "database": str(args.db),
        "manifest": str(args.manifest),
        "readme": str(args.readme),
        "validation": checks,
    }, ensure_ascii=True, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
