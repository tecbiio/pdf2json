from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Iterable, List, Optional

import pdfplumber


def parse_number(raw: str) -> Optional[float]:
  """Retourne un float si la chaîne ressemble à un nombre, sinon None."""
  if not raw:
    return None
  cleaned = re.sub(r"[^\d,.\-]", "", raw)
  if not cleaned:
    return None
  normalized = cleaned.replace(",", ".")
  try:
    return float(normalized)
  except ValueError:
    return None


def is_number_token(token: str) -> bool:
  return bool(re.fullmatch(r"-?\d+(?:[.,]\d+)?", token))


def is_percent_token(token: str) -> bool:
  return bool(re.fullmatch(r"\d+(?:,\d+)?%", token))


def parse_invoice_line(text: str) -> Optional[dict]:
  """Parse une ligne de facture formatée : REF DESC QTE PU MONTANT [TVA%]."""
  tokens = text.split()
  if len(tokens) < 5:
    return None

  tva_token = tokens[-1] if is_percent_token(tokens[-1]) else None
  if tva_token:
    tokens = tokens[:-1]

  if len(tokens) < 4:
    return None

  amount_tok = tokens[-1]
  unit_tok = tokens[-2]
  qty_tok = tokens[-3]

  if not all(is_number_token(tok) for tok in (amount_tok, unit_tok, qty_tok)):
    return None
  if not ("," in unit_tok or "." in unit_tok):
    return None
  if not ("," in amount_tok or "." in amount_tok):
    return None

  ref = tokens[0]
  if not any(ch.isdigit() for ch in ref):
    return None
  desc_tokens = tokens[1:-3]
  if not desc_tokens:
    return None

  description = " ".join(desc_tokens)
  return {
    "reference": ref,
    "description": description,
    "quantity": parse_number(qty_tok),
    "unit_price": parse_number(unit_tok),
    "line_total": parse_number(amount_tok),
    "tva": tva_token,
    "raw": text,
  }


def looks_like_reference_line(text: str) -> bool:
  """Heuristique : commence par un identifiant contenant au moins un chiffre."""
  tokens = text.split()
  if not tokens:
    return False
  first = tokens[0]
  return any(ch.isdigit() for ch in first)


def extract_invoice_lines(pdf_path: Path, template_type: str = "facture") -> List[dict]:
  """Extrait les lignes de facture basées sur une lecture texte pdfplumber."""
  pdf_path = Path(pdf_path)
  if not pdf_path.exists():
    raise FileNotFoundError(f"PDF not found: {pdf_path}")

  lines: List[dict] = []
  with pdfplumber.open(pdf_path) as pdf:
    for page_index, page in enumerate(pdf.pages, start=1):
      text = page.extract_text() or ""
      buffer = ""
      row_counter = 0
      for text_line in text.splitlines():
        cleaned_line = text_line.strip()
        if not cleaned_line:
          continue

        if looks_like_reference_line(cleaned_line):
          candidate = cleaned_line
          buffer = ""
        else:
          candidate = f"{buffer} {cleaned_line}".strip() if buffer else cleaned_line

        ignore_parts = [
          "REFERENCE DESIGNATION",
          "Bon de livraison",
          "Commande client",
          "Sous Total",
          "Total TTC",
          "Total HT",
          "RIB",
        ]
        if template_type.lower() == "facture":
          ignore_parts.append("Facture")
        elif template_type.lower() == "avoir":
          ignore_parts.append("Avoir")
        else:
          ignore_parts.extend(["Facture", "Avoir"])

        if re.search("|".join(ignore_parts), candidate, re.IGNORECASE):
          buffer = ""
          continue

        parsed = parse_invoice_line(candidate)

        if parsed:
          row_counter += 1
          lines.append(
            {
              "page": page_index,
              "row": row_counter,
              "columns": [{"index": 0, "value": candidate}],
              "payload": {
                "reference": parsed["reference"],
                "description": parsed["description"],
                "quantity": parsed["quantity"],
                "unit_price": parsed["unit_price"],
                "line_total": parsed["line_total"],
                "tva": parsed["tva"],
              },
              "raw": parsed["raw"],
            }
          )
          buffer = ""
        else:
          buffer = candidate
  return lines


def write_output(lines: Iterable[dict], output_path: Optional[Path], ndjson: bool) -> None:
  serialized = "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) if ndjson else json.dumps(list(lines), ensure_ascii=False, indent=2)
  if output_path:
    output_path = Path(output_path)
    output_path.write_text(serialized, encoding="utf-8")
  else:
    print(serialized)


def write_csv(lines: List[dict], csv_path: Path) -> None:
  csv_path = Path(csv_path)
  fieldnames = [
    "page",
    "row",
    "reference",
    "description",
    "quantity",
    "unit_price",
    "line_total",
    "tva",
  ]
  with csv_path.open("w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    for line in lines:
      payload = line.get("payload", {})
      writer.writerow(
        {
          "page": line.get("page"),
          "row": line.get("row"),
          "reference": payload.get("reference"),
          "description": payload.get("description"),
          "quantity": payload.get("quantity"),
          "unit_price": payload.get("unit_price"),
          "line_total": payload.get("line_total"),
          "tva": payload.get("tva"),
        }
      )


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description="Extract invoice lines from a PDF and emit JSON payloads.")
  parser.add_argument("pdf_path", type=Path, help="Chemin vers le fichier PDF de la facture")
  parser.add_argument(
    "--template-type",
    choices=["facture", "avoir"],
    default="facture",
    help="Type de template à utiliser (facture ou avoir).",
  )
  parser.add_argument(
    "--ndjson",
    action="store_true",
    help="Écrit un JSON par ligne (format NDJSON) plutôt qu'une liste JSON.",
  )
  parser.add_argument(
    "--csv",
    type=Path,
    help="Chemin d'un fichier CSV pour les lignes extraites (utile pour des tests).",
  )
  return parser.parse_args()


def main() -> None:
  args = parse_args()
  lines = extract_invoice_lines(args.pdf_path, template_type=args.template_type)
  if args.csv:
    write_csv(lines, args.csv)
  else:
    write_output(lines, None, args.ndjson)


if __name__ == "__main__":
  main()
