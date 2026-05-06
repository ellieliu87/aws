"""Knowledge Base router — uploaded documents the `rag_search` tool queries.

Files land under `CMA_DOCS_ROOT/uploads/` (default
`<repo>/sample_docs/uploads/`) and become searchable by the universal
`rag_search` built-in. Supported formats: `.md .txt .pdf .docx .pptx
.py .csv .xlsx .xls .json`. Optional `scope` puts the file in a
sub-folder so analysts can group whitepapers by domain
(`retail_deposit`, `portfolio`, etc.).

Storage is on the local filesystem and **non-versioned** — production
deployments should swap this for an S3 / OneLake-backed store.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from routers.auth import get_current_user

router = APIRouter()


ALLOWED_EXTENSIONS = {
    ".md", ".txt", ".pdf", ".docx", ".pptx",
    ".py", ".csv", ".xlsx", ".xls", ".json",
}


def _docs_root() -> Path:
    """Resolve the on-disk root for uploaded knowledge-base documents.

    Honors `CMA_DOCS_ROOT` if set; otherwise defaults to
    `<repo>/sample_docs/uploads/`. The same `sample_docs/` parent
    contains the bundled retail-deposit / portfolio whitepapers, so
    `rag_search` (which walks recursively from `sample_docs/`)
    automatically picks up uploads alongside the curated corpus.
    """
    env = os.environ.get("CMA_DOCS_ROOT", "").strip()
    base = Path(env) if env else Path(__file__).resolve().parent.parent.parent / "sample_docs"
    return base / "uploads"


class DocumentInfo(BaseModel):
    id: str             # path relative to docs root, forward-slash separated
    name: str           # base filename
    size_bytes: int
    extension: str      # lowercase, includes leading dot
    scope: str | None = None  # subfolder path, or None for root
    uploaded_at: str    # ISO-8601 UTC


def _scan_docs() -> list[DocumentInfo]:
    root = _docs_root()
    if not root.exists():
        return []
    out: list[DocumentInfo] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            continue
        try:
            stat = p.stat()
        except OSError:
            continue
        rel = p.relative_to(root)
        scope_parts = rel.parts[:-1]
        scope = "/".join(scope_parts) if scope_parts else None
        out.append(DocumentInfo(
            id=str(rel).replace("\\", "/"),
            name=p.name,
            size_bytes=stat.st_size,
            extension=ext,
            scope=scope,
            uploaded_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        ))
    return out


def _resolve_within_root(rel_id: str) -> Path:
    """Resolve `rel_id` relative to docs root and reject traversal attempts."""
    root = _docs_root().resolve()
    target = (root / rel_id).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes docs root")
    return target


@router.get("", response_model=list[DocumentInfo])
async def list_documents(_: str = Depends(get_current_user)):
    """List all uploaded documents under the knowledge-base root."""
    return _scan_docs()


@router.get("/root")
async def get_docs_root(_: str = Depends(get_current_user)):
    """Return the configured on-disk root + supported extensions.

    Surfacing this lets the frontend show analysts where uploads land
    so they can drop additional files via the host filesystem if they
    prefer (useful for batch transfers and corporate scan tools)."""
    root = _docs_root()
    return {
        "root":        str(root),
        "exists":      root.exists(),
        "extensions":  sorted(ALLOWED_EXTENSIONS),
        "env_var":     "CMA_DOCS_ROOT",
        "env_value":   os.environ.get("CMA_DOCS_ROOT", "") or None,
    }


@router.post("/upload", response_model=DocumentInfo)
async def upload_document(
    file: UploadFile = File(...),
    scope: str | None = Form(None),
    _: str = Depends(get_current_user),
):
    """Upload a single document.

    `scope` is an optional sub-folder name (e.g. `retail_deposit`) so
    analysts can keep related whitepapers together. The agent reads the
    same physical folder via `rag_search`, so the scope is also a
    natural pre-filter the analyst can pass to the tool.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="file has no filename")
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type {ext!r}. Allowed: {sorted(ALLOWED_EXTENSIONS)}",
        )

    root = _docs_root()
    # Sanitize the scope to a single-level safe folder name.
    safe_scope = None
    if scope:
        safe_scope = scope.strip().strip("/").replace("\\", "/")
        # Drop any traversal attempts; keep only [a-zA-Z0-9_-/]+.
        if any(seg in {"", ".", ".."} for seg in safe_scope.split("/")):
            raise HTTPException(status_code=400, detail="invalid scope")
    target_dir = (root / safe_scope) if safe_scope else root
    target_dir.mkdir(parents=True, exist_ok=True)

    safe_name = Path(file.filename).name  # strip any path components from upload
    target_path = target_dir / safe_name
    if target_path.exists():
        # Avoid overwrite — append a short uuid suffix.
        target_path = target_dir / f"{target_path.stem}-{uuid.uuid4().hex[:8]}{ext}"

    contents = await file.read()
    target_path.write_bytes(contents)

    rel = target_path.relative_to(root)
    return DocumentInfo(
        id=str(rel).replace("\\", "/"),
        name=target_path.name,
        size_bytes=len(contents),
        extension=ext,
        scope=safe_scope,
        uploaded_at=datetime.now(timezone.utc).isoformat(),
    )


@router.delete("/{doc_id:path}")
async def delete_document(doc_id: str, _: str = Depends(get_current_user)):
    """Delete an uploaded document by its id (= relative path)."""
    target = _resolve_within_root(doc_id)
    if not target.exists():
        raise HTTPException(status_code=404, detail="document not found")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="not a file")
    target.unlink()
    return {"ok": True, "id": doc_id}


# ── Content fetch (for the preview panel) ─────────────────────────────────
@router.get("/content/{doc_id:path}")
async def get_document_content(doc_id: str, _: str = Depends(get_current_user)):
    """Return a document's raw text content. Used by the KB preview panel
    for markdown render. Binary formats (pdf, docx) return decoded text
    via the same lazy-imported helpers `rag_search` uses, so the analyst
    can preview the agent-extractable content (not the binary bytes)."""
    target = _resolve_within_root(doc_id)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="document not found")
    text = _extract_text(target)
    return {"id": doc_id, "name": target.name, "extension": target.suffix.lower(), "text": text}


def _extract_text(p: Path) -> str:
    """Lazy text extraction across formats. Mirrors the logic in
    rag_search so the preview matches what the agent sees."""
    ext = p.suffix.lower()
    try:
        if ext in (".md", ".txt", ".py", ".json"):
            return p.read_text(encoding="utf-8", errors="ignore")
        if ext == ".csv":
            import pandas as pd
            return pd.read_csv(p).to_csv(index=False)
        if ext in (".xlsx", ".xls"):
            import pandas as pd
            return pd.read_excel(p).to_csv(index=False)
        if ext == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                return "(pypdf not installed; cannot extract PDF text)"
            reader = PdfReader(str(p))
            return "\n\n".join((page.extract_text() or "") for page in reader.pages)
        if ext == ".docx":
            try:
                from docx import Document  # python-docx
            except ImportError:
                return "(python-docx not installed; cannot extract Word text)"
            doc = Document(str(p))
            paragraphs = [para.text for para in doc.paragraphs if para.text and para.text.strip()]
            for tbl in doc.tables:
                for row in tbl.rows:
                    for cell in row.cells:
                        if cell.text and cell.text.strip():
                            paragraphs.append(cell.text.strip())
            return "\n\n".join(paragraphs)
        if ext == ".pptx":
            try:
                from pptx import Presentation  # python-pptx
            except ImportError:
                return "(python-pptx not installed; cannot extract slide text)"
            prs = Presentation(str(p))
            slides_out: list[str] = []
            for idx, slide in enumerate(prs.slides, start=1):
                parts = [f"[Slide {idx}]"]
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        for para in shape.text_frame.paragraphs:
                            txt = para.text.strip() if para.text else ""
                            if txt:
                                parts.append(txt)
                slides_out.append("\n".join(parts))
            return "\n\n".join(slides_out)
        return "(unsupported format for preview)"
    except Exception as e:
        return f"(extraction failed: {e})"


# ── Whitepaper extraction (LLM-driven) ────────────────────────────────────
_EXTRACT_SYSTEM = """You convert raw model-documentation text (extracted from a PDF or
Word file) into a structured methodology whitepaper in markdown.

Your output MUST follow this template exactly:

```
---
model_id: <SHORT_ID like PRED_PORTFOLIO_COMPONENT or omit if unclear>
model_component: <short label, e.g. "Backbook Balance (retention)">
portfolio_scope: <comma-separated portfolios, e.g. "Legacy_COF, Discover_DFS">
suite_role: <one short phrase: Volume / Pricing / Internal / Overlay + sub-role>
last_updated: <YYYY-MM-DD — today's date if not stated in source>
---

# <Model Display Name>

<2–3 sentence description of the model's purpose and scope. What does it
project, for whom, and where does it sit in the broader suite.>

## Methodology

<Concrete formula in a fenced code block when one is given. List
calibrated parameters in a markdown table when shown. Cite assumptions
in plain prose. Keep each subsection tight — analysts read this, not
generals.>

## Stress overlay

<What changes under regulatory stress (BHC Stress / Fed SA). Be specific
about which inputs flip and by how much. Skip this section if the
source has no stress overlay.>

## Suite linkages

<Bulleted list:
- **Inputs**: which upstream models feed this one
- **Outputs feed**: which downstream models / decisions consume this one
- **Special interactions**: any cross-model coupling worth flagging>

## Caveats

<Known limitations, data constraints, and out-of-scope behaviors. One
bullet per caveat.>
```

Rules:
- ONLY emit the markdown above. No preamble, no commentary, no closing
  remarks. The first character of your response must be `---`.
- Preserve concrete numbers, formulas, and parameters from the source
  verbatim — don't paraphrase them away.
- If the source is missing a section (e.g. no stress overlay), drop
  that H2 entirely rather than writing "N/A" — the reader knows.
- If a piece of frontmatter isn't in the source, omit that line. Don't
  invent values.
- Keep the whole output under 1500 words; trim repetitive prose to fit
  but keep every formula and parameter table.
"""


@router.post("/extract-whitepaper", response_model=DocumentInfo)
async def extract_whitepaper(
    file: UploadFile = File(...),
    scope: str | None = Form(None),
    title: str | None = Form(None),
    focus_areas: str | None = Form(None),
    _: str = Depends(get_current_user),
):
    """Upload a PDF or Word doc, run an extractor LLM that produces a
    structured methodology whitepaper in markdown, save it under the
    knowledge base, and return the new DocumentInfo. The output mirrors
    the bundled `sample_docs/retail_deposit/PRED_*.md` template so the
    extracted file slots seamlessly into the existing rag_search corpus."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="file has no filename")
    ext = Path(file.filename).suffix.lower()
    if ext not in (".pdf", ".docx"):
        raise HTTPException(
            status_code=400,
            detail=f"Extraction only supports .pdf and .docx (got {ext!r}). "
                   "For .md uploads, use the regular Upload Files button.",
        )

    # Persist the source temporarily so _extract_text can run.
    import tempfile
    contents = await file.read()
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        tmp.write(contents)
        tmp_path = Path(tmp.name)
    try:
        raw_text = _extract_text(tmp_path)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    if not raw_text.strip() or raw_text.startswith("("):
        raise HTTPException(
            status_code=400,
            detail=f"Couldn't extract text from {file.filename}: {raw_text}",
        )

    # Truncate very long source to fit context window (~12K chars ≈ 3K tokens).
    if len(raw_text) > 12000:
        raw_text = raw_text[:12000] + "\n\n[... source truncated for extraction ...]"

    # LLM call — same OpenAI client construction the analytics_defs draft
    # endpoint uses, so we inherit the corporate-proxy compat.
    try:
        from openai import AsyncOpenAI
    except ImportError:
        raise HTTPException(status_code=503, detail="openai package not installed")
    from cof.llm_config import resolve_model
    client = AsyncOpenAI()

    user_prompt = (
        "Extract a structured methodology whitepaper from the source text below. "
        "Follow the template in the system prompt exactly.\n\n"
        f"Source filename: {file.filename}\n"
    )
    if title:
        user_prompt += f"Suggested model title: {title.strip()}\n"
    # Analyst-supplied focus areas — what they want the extractor to
    # prioritize. This rides on top of the always-required template
    # (frontmatter + Methodology / Stress overlay / Suite linkages /
    # Caveats); the analyst can add domain-specific bullets, ask for
    # extra emphasis on parameter tables, or trim the default list.
    if focus_areas and focus_areas.strip():
        user_prompt += (
            "\nThe analyst specifically asked you to extract / emphasize:\n"
            f"{focus_areas.strip()}\n"
            "Cover these in addition to the standard template sections; "
            "do NOT skip the standard sections to make room for them.\n"
        )
    user_prompt += f"\n--- SOURCE TEXT ---\n{raw_text}\n--- END SOURCE TEXT ---"

    try:
        completion = await client.chat.completions.create(
            model=resolve_model(os.getenv("CMA_TOOL_DRAFT_MODEL")),
            messages=[
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.2,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}")

    md_text = (completion.choices[0].message.content or "").strip()
    if not md_text:
        raise HTTPException(status_code=502, detail="LLM returned empty extraction")
    # Some models wrap the whole response in a fenced ```markdown block.
    if md_text.startswith("```"):
        # Strip the opening fence + closing fence
        first_nl = md_text.find("\n")
        if first_nl != -1:
            md_text = md_text[first_nl + 1:]
        if md_text.rstrip().endswith("```"):
            md_text = md_text.rstrip()[:-3].rstrip()

    # Persist as .md under chosen scope (default: whitepapers/).
    safe_scope = scope.strip().strip("/").replace("\\", "/") if scope else "whitepapers"
    if any(seg in {"", ".", ".."} for seg in safe_scope.split("/")):
        raise HTTPException(status_code=400, detail="invalid scope")
    target_dir = _docs_root() / safe_scope
    target_dir.mkdir(parents=True, exist_ok=True)

    stem = Path(file.filename).stem
    target_path = target_dir / f"{stem}.md"
    if target_path.exists():
        target_path = target_dir / f"{stem}-{uuid.uuid4().hex[:8]}.md"
    target_path.write_text(md_text, encoding="utf-8")

    rel = target_path.relative_to(_docs_root())
    return DocumentInfo(
        id=str(rel).replace("\\", "/"),
        name=target_path.name,
        size_bytes=len(md_text.encode("utf-8")),
        extension=".md",
        scope=safe_scope,
        uploaded_at=datetime.now(timezone.utc).isoformat(),
    )
