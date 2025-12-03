import base64
import tempfile
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from invoice_parser import extract_invoice_lines

app = FastAPI(title="pdf2json-parser", version="1.0.0")


class ParseRequest(BaseModel):
    docType: str
    fileBase64: str
    fileName: Optional[str] = None


class ParsedLine(BaseModel):
    reference: str
    description: Optional[str] = None
    quantity: float


class ParseResponse(BaseModel):
    lines: List[ParsedLine]


def map_doc_type(doc_type: str) -> str:
    doc_type = (doc_type or "").lower()
    if doc_type == "avoir":
        return "avoir"
    return "facture"


@app.post("/parse", response_model=ParseResponse)
def parse_pdf(payload: ParseRequest):
    try:
        pdf_bytes = base64.b64decode(payload.fileBase64)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid base64: {exc}")

    template_type = map_doc_type(payload.docType)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        tmp.write(pdf_bytes)
        tmp.flush()
        lines = extract_invoice_lines(Path(tmp.name), template_type=template_type)

    parsed: List[ParsedLine] = []
    for line in lines:
        ref = line.get("payload", {}).get("reference") or ""
        qty = line.get("payload", {}).get("quantity")
        desc = line.get("payload", {}).get("description") or ""
        if not ref or qty is None:
            continue
        parsed.append(ParsedLine(reference=str(ref), description=desc, quantity=float(qty)))

    return ParseResponse(lines=parsed)


@app.get("/health")
def health():
    return {"ok": True}
