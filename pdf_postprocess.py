#!/usr/bin/env python3
"""pdf_postprocess.py — write PDF metadata through supported APIs, and
validate the result with every parser available.

An earlier build set the document catalog by assigning Python values
straight onto reportlab's internal catalog object:

    cv._doc.Catalog.Lang = "en-US"

which serialized as `/Lang en-US` — a bare token where the format
requires a string object. Poppler tolerated it and reported "Tagged:
yes"; pypdf could not find /Root at all. A file that renders is not a
valid file, so metadata now goes through pikepdf's object model, which
cannot emit a malformed elementary object.

Accessibility: reportlab (4.5) emits no marked content, so there are no
MCIDs for a structure tree to reference. Declaring /MarkInfo /Marked
true over unmarked content streams claims a conformance the file does
not have. This module therefore writes a structurally valid UNTAGGED
document and reports `accessibility: "untagged"` rather than shipping
tagging metadata that no validator would accept.
"""

import io
import json


ACCESS_UNTAGGED = "untagged"
ACCESS_TAGGED = "tagged"


def finalize(pdf_bytes, title=None, author=None, subject=None,
             lang="en-US", keywords=None):
    """Attach document metadata and return (bytes, status).

    Everything is written through pikepdf's typed object model: strings
    become PDF strings, names become PDF names. Nothing is injected as
    raw syntax.
    """
    status = {"metadata_written": False, "accessibility": ACCESS_UNTAGGED,
              "accessibility_reason": (
                  "reportlab does not emit marked content, so no structure "
                  "tree can reference it; shipping /MarkInfo over unmarked "
                  "content would claim conformance the file lacks"),
              "lang": None, "error": None}
    try:
        import pikepdf
    except ImportError as e:
        status["error"] = "pikepdf unavailable: %s" % e
        return pdf_bytes, status

    try:
        with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
            # /Lang is a text string object, not a bare token
            if lang:
                pdf.Root.Lang = pikepdf.String(lang)
                status["lang"] = lang
            pdf.Root.ViewerPreferences = pikepdf.Dictionary(
                DisplayDocTitle=True)
            with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
                if title:
                    meta["dc:title"] = title
                if author:
                    meta["dc:creator"] = [author]
                if subject:
                    meta["dc:description"] = subject
                if lang:
                    meta["dc:language"] = [lang]
                if keywords:
                    meta["pdf:Keywords"] = keywords
            with pdf.open_metadata() as _:
                pass
            di = pdf.docinfo
            if title:
                di["/Title"] = title
            if author:
                di["/Author"] = author
            if subject:
                di["/Subject"] = subject
            out = io.BytesIO()
            pdf.save(out, linearize=False)
            status["metadata_written"] = True
            return out.getvalue(), status
    except Exception as e:
        status["error"] = "%s: %s" % (type(e).__name__, e)
        return pdf_bytes, status


# ── validation ──────────────────────────────────────────────────────────

def validate(pdf_bytes):
    """Run every checker present. Reports which ran and which did not —
    an absent validator is recorded as 'unavailable', never as a pass."""
    rep = {"checks": {}, "ok": True, "pages": None}

    # 1. qpdf structural check (via pikepdf's libqpdf binding)
    try:
        import pikepdf
        with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
            # libqpdf's syntax check — the same engine `qpdf --check` uses
            problems = list(pdf.check_pdf_syntax())
            rep["pages"] = len(pdf.pages)
            rep["checks"]["qpdf_check"] = {
                "status": "pass" if not problems else "fail",
                "libqpdf": pikepdf.__libqpdf_version__,
                "problems": list(problems)[:10],
            }
            if problems:
                rep["ok"] = False
    except ImportError:
        rep["checks"]["qpdf_check"] = {"status": "unavailable",
                                       "detail": "pikepdf not installed"}
    except Exception as e:
        rep["checks"]["qpdf_check"] = {"status": "fail",
                                       "detail": "%s: %s" % (type(e).__name__, e)}
        rep["ok"] = False

    # 2. pypdf — must open with no exception AND no warnings
    try:
        import warnings
        import pypdf
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            r = pypdf.PdfReader(io.BytesIO(pdf_bytes))
            n = len(r.pages)
            _ = r.metadata
            for p in r.pages:
                p.extract_text()
        msgs = [str(x.message)[:200] for x in w]
        rep["checks"]["pypdf"] = {
            "status": "pass" if not msgs else "fail",
            "version": pypdf.__version__, "pages": n, "warnings": msgs[:8]}
        if msgs:
            rep["ok"] = False
    except ImportError:
        rep["checks"]["pypdf"] = {"status": "unavailable",
                                  "detail": "pypdf not installed"}
    except Exception as e:
        rep["checks"]["pypdf"] = {"status": "fail",
                                  "detail": "%s: %s" % (type(e).__name__, e)}
        rep["ok"] = False

    # 3. page rendering (PyMuPDF stands in for poppler's renderer)
    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        rendered = 0
        for i in range(doc.page_count):
            pm = doc[i].get_pixmap(dpi=72)
            if pm.width and pm.height:
                rendered += 1
        doc.close()
        rep["checks"]["render"] = {
            "status": "pass" if rendered == (rep["pages"] or rendered)
                      else "fail",
            "pages_rendered": rendered, "engine": "pymupdf"}
        if rep["pages"] and rendered != rep["pages"]:
            rep["ok"] = False
    except ImportError:
        rep["checks"]["render"] = {"status": "unavailable",
                                   "detail": "pymupdf not installed"}
    except Exception as e:
        rep["checks"]["render"] = {"status": "fail",
                                   "detail": "%s: %s" % (type(e).__name__, e)}
        rep["ok"] = False

    # 4. poppler CLI, if it happens to be on PATH
    rep["checks"]["pdfinfo"] = _cli_check(pdf_bytes, "pdfinfo")
    # 5. PDF/UA validator, if installed
    rep["checks"]["verapdf"] = _cli_check(pdf_bytes, "verapdf",
                                          args=["--format", "text"])
    return rep


def _cli_check(pdf_bytes, exe, args=None):
    import os
    import shutil
    import subprocess
    import tempfile
    path = shutil.which(exe)
    if not path:
        return {"status": "unavailable",
                "detail": "%s not on PATH" % exe}
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        tmp.write(pdf_bytes)
        tmp.close()
        p = subprocess.run([path] + (args or []) + [tmp.name],
                           capture_output=True, text=True, timeout=120)
        return {"status": "pass" if p.returncode == 0 else "fail",
                "returncode": p.returncode,
                "stdout": (p.stdout or "")[:600],
                "stderr": (p.stderr or "")[:400]}
    except Exception as e:
        return {"status": "fail", "detail": "%s: %s" % (type(e).__name__, e)}
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def main():
    import sys
    for p in sys.argv[1:]:
        with open(p, "rb") as fh:
            rep = validate(fh.read())
        print(json.dumps({p: rep}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
