from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import pdfplumber
import requests


def clean_cell(value: Optional[str]) -> str:
    """Normalize a table cell value to a single trimmed line."""
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def parse_number(raw: str) -> Optional[float]:
    """Return a float if the string looks numeric, otherwise None."""
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
    """Parse a single invoice line formatted as: REF DESC QTE PU MONTANT [TVA%]."""
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
    """Heuristic: commence par un identifiant contenant au moins un chiffre."""
    tokens = text.split()
    if not tokens:
        return False
    first = tokens[0]
    return any(ch.isdigit() for ch in first)


def build_payload(columns: List[str]) -> dict:
    """Create a simple JSON-ready payload for a line."""
    numbers = []
    description = ""
    for col in columns:
        number = parse_number(col)
        if number is not None:
            numbers.append(number)
        elif not description:
            description = col

    quantity = numbers[0] if numbers else None
    unit_price = numbers[1] if len(numbers) > 1 else None
    line_total = None
    if len(numbers) > 2:
        line_total = numbers[-1]
    elif len(numbers) == 2:
        line_total = numbers[1]
    elif len(numbers) == 1:
        line_total = numbers[0]

    return {
        "description": description or (columns[0] if columns else ""),
        "quantity": quantity,
        "unit_price": unit_price,
        "line_total": line_total,
    }


def extract_invoice_lines(pdf_path: Path, template_type: str = "facture") -> List[dict]:
    """Extract invoice lines based on text parsing tailored to the template."""
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

                # Si la ligne ressemble à une nouvelle référence, on repart de zéro (pour éviter de polluer avec l'en-tête).
                if looks_like_reference_line(cleaned_line):
                    candidate = cleaned_line
                    buffer = ""
                else:
                    candidate = f"{buffer} {cleaned_line}".strip() if buffer else cleaned_line

                # Ignore les lignes d'en-tête répandues ou les totaux/pied de page.
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
                    # Accumule une ligne trop longue qui déborde sur la suivante (description en plusieurs lignes).
                    buffer = candidate
    return lines


def fetch_reference_id(
    lookup_url: Optional[str], reference: str, headers: dict, verbose: bool = False
) -> Tuple[Optional[str], str, Optional[str]]:
    """Call the lookup endpoint to retrieve an ID from a reference."""
    if not lookup_url:
        return None, "skipped_no_lookup_url", None
    url = lookup_url.replace("{reference}", reference)
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as exc:
        return None, "http_error", str(exc)

    # Tentative simple: champ "id" dans la réponse JSON ou premier élément d'une liste.
    try:
        data = resp.json()
    except Exception as exc:
        return None, "invalid_json", str(exc)

    if isinstance(data, dict):
        if "id" in data:
            return str(data["id"]), "ok", None
        if "data" in data and isinstance(data["data"], dict) and "id" in data["data"]:
            return str(data["data"]["id"]), "ok", None
        if "data" in data and isinstance(data["data"], list) and data["data"]:
            first = data["data"][0]
            if isinstance(first, dict) and "id" in first:
                return str(first["id"]), "ok", None
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict) and "id" in first:
            return str(first["id"]), "ok", None
    return None, "no_id", None


def patch_stock(
    update_url_template: Optional[str],
    product_id: str,
    new_stock: float,
    reason: str,
    headers: dict,
) -> bool:
    """Send a PATCH to update stock; returns True on success."""
    if not update_url_template:
        return False
    url = update_url_template.replace("{product_id}", str(product_id))
    payload = {"stock": new_stock, "update_reason": reason}
    try:
        resp = requests.patch(url, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        return True
    except Exception:
        return False


def load_lookup_url(config_path: Optional[Path]) -> Optional[str]:
    if not config_path:
        return None
    cfg_path = Path(config_path)
    if not cfg_path.exists():
        return None
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict):
        if "lookup_url" in data:
            return str(data["lookup_url"])
        if "lookup_products_url" in data:
            return str(data["lookup_products_url"])
    return None


def load_api_key(api_key_path: Path) -> Optional[str]:
    path = Path(api_key_path)
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8").strip()
    return content or None


def load_products_url(config_path: Optional[Path], fallback_lookup_url: Optional[str]) -> Optional[str]:
    if config_path:
        cfg_path = Path(config_path)
        if cfg_path.exists():
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    if "products_url" in data:
                        return str(data["products_url"])
                    if "lookup_products_url" in data:
                        # Si aucun endpoint dédié, on tente la version sans param.
                        url = str(data["lookup_products_url"])
                        if "?" in url:
                            return url.split("?", 1)[0]
                        return url
            except Exception:
                pass
    # Fallback: utiliser la partie avant le ? du lookup_url si presente.
    if fallback_lookup_url:
        if "?" in fallback_lookup_url:
            return fallback_lookup_url.split("?", 1)[0]
        return fallback_lookup_url
    return None


def load_update_url(config_path: Optional[Path]) -> Optional[str]:
    if not config_path:
        return None
    cfg_path = Path(config_path)
    if not cfg_path.exists():
        return None
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict):
        if "update_product_stock_url" in data:
            return str(data["update_product_stock_url"])
    return None


def extract_invoice_number(pdf_path: Path, template_type: str) -> Optional[str]:
    """Extrait le numero de facture/avoir depuis la premiere page."""
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        return None
    try:
        with pdfplumber.open(pdf_path) as pdf:
            first_page = pdf.pages[0]
            text = first_page.extract_text() or ""
    except Exception:
        return None

    if template_type.lower() == "facture":
        match = re.search(r"Facture\s+N[°º]?\s*([A-Z0-9_]+)", text, re.IGNORECASE)
    else:
        match = re.search(r"Avoir\s+N[°º]?\s*([A-Z0-9_]+)", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def fetch_products_catalog(
    products_url: Optional[str],
    headers: dict,
    cache_path: Path,
    refresh: bool = False,
    verbose: bool = False,
) -> List[dict]:
    """Recupere tous les produits via pagination et met en cache."""
    cache_path = Path(cache_path)
    if cache_path.exists() and not refresh:
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    if not products_url:
        return []

    # Appel initial pour obtenir metadata (results / results_per_page) même si 403.
    meta_data = {}
    try:
        meta_resp = requests.get(products_url, headers=headers, timeout=15)
        meta_data = meta_resp.json()
    except Exception:
        meta_data = {}
    # Certaines API renvoient les infos dans meta_data['error']
    meta_block = meta_data.get("error") if isinstance(meta_data, dict) and "error" in meta_data else meta_data

    total = meta_block.get("results") if isinstance(meta_block, dict) else None
    per_page = None
    if isinstance(meta_block, dict):
        per_page = meta_block.get("results_per_page") or meta_block.get("results_perpage")
    pages = meta_block.get("pages") if isinstance(meta_block, dict) else None

    products: List[dict] = []

    if not pages:
        if total and per_page:
            pages = max(1, math.ceil(total / per_page))
        else:
            pages = 1  # fallback si metadata manquante

    for page in range(1, pages + 1):
        try:
            hdrs = dict(headers)
            hdrs.setdefault("page", str(page))  # certains endpoints demandent un header page
            resp = requests.get(products_url, headers=hdrs, params={"page": page}, timeout=15)
            try:
                data = resp.json()
            except Exception:
                continue
            if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                products.extend(data["data"])
            elif isinstance(data, list):
                products.extend(data)
        except Exception:
            continue

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(products, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    return products


def build_product_index(products: List[dict]) -> dict:
    """Construit un index {reference-> {id, stock}}."""
    index = {}
    for p in products:
        code = p.get("product_code") or p.get("code") or p.get("reference")
        pid = p.get("id") or p.get("product_id")
        if code and pid:
            stock = p.get("stock")
            if stock is None:
                stock = p.get("quantity") or p.get("stock_quantity") or p.get("stock_level")
            index[str(code).strip()] = {"id": str(pid), "stock": stock}
    return index


def log_stock_event(log_path: Path, entry: dict) -> None:
    """Append a JSON line into gen/ logs for stock updates."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


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
        "lookup_id",
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
                    "lookup_id": payload.get("lookup_id"),
                }
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract invoice lines from a PDF and emit JSON payloads ready for an API body."
    )
    parser.add_argument("pdf_path", type=Path, help="Chemin vers le fichier PDF de la facture")
    parser.add_argument(
        "--template-type",
        choices=["facture", "avoir"],
        default="facture",
        help="Type de template a utiliser (facture ou avoir).",
    )
    parser.add_argument(
        "--ndjson",
        action="store_true",
        help="Ecrit un JSON par ligne (format NDJSON) plutot qu'une liste JSON.",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        help="Chemin d'un fichier CSV pour les lignes extraites (utile pour des tests).",
    )
    parser.add_argument(
        "--verbose-lookups",
        action="store_true",
        help="Ajoute des infos de debug sur les lookups (statut, message) dans la sortie.",
    )
    parser.add_argument(
        "--verbose-products",
        action="store_true",
        help="Log detaille sur le fetch des produits (cache, pagination).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.json"),
        help="Fichier JSON contenant lookup_url/lookup_products_url et update_product_stock_url (defaut: config.json si present).",
    )
    parser.add_argument(
        "--api-key-path",
        type=Path,
        default=Path("utils/api_key.txt"),
        help="Fichier texte contenant la cle API pour le header userApiKey.",
    )
    parser.add_argument(
        "--update-stock",
        action="store_true",
        help="Envoie un PATCH pour mettre a jour le stock selon le template (facture decremente, avoir incremente).",
    )
    parser.add_argument(
        "--update-reason",
        type=str,
        default="sync from pdf",
        help="Raison transmise au PATCH stock (update_reason).",
    )
    parser.add_argument(
        "--products-cache",
        type=Path,
        default=Path(".cache/products.json"),
        help="Fichier cache local des produits (pour limiter les appels).",
    )
    parser.add_argument(
        "--refresh-products",
        action="store_true",
        help="Force le rafraichissement du cache produits.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    lookup_url = load_lookup_url(args.config)
    products_url = load_products_url(args.config, lookup_url)
    update_url = load_update_url(args.config)
    invoice_number = extract_invoice_number(args.pdf_path, args.template_type)

    headers: dict[str, str] = {}

    api_key = load_api_key(args.api_key_path)
    if api_key and "userApiKey" not in headers:
        headers["userApiKey"] = api_key

    product_index = {}
    if products_url:
        products = fetch_products_catalog(
            products_url,
            headers,
            cache_path=args.products_cache,
            refresh=args.refresh_products,
            verbose=args.verbose_products,
        )
        product_index = build_product_index(products)

    lines = extract_invoice_lines(args.pdf_path, template_type=args.template_type)

    if lookup_url:
        for line in lines:
            payload = line.get("payload", {})
            ref = payload.get("reference")
            if not ref:
                if args.verbose_lookups:
                    payload["lookup_status"] = "skipped_no_reference"
                continue
            lookup_id = None

            # D'abord le cache local
            if product_index:
                entry = product_index.get(str(ref).strip())
                if entry:
                    lookup_id = entry.get("id")
                    if lookup_id and args.verbose_lookups:
                        payload["lookup_status"] = "from_cache"
                    if entry.get("stock") is not None:
                        payload["initial_stock"] = entry.get("stock")

            # Ensuite appel réseau si pas trouvé et URL dispo
            if not lookup_id and lookup_url:
                lookup_id, status, info = fetch_reference_id(lookup_url, ref, headers, verbose=args.verbose_lookups)
                if args.verbose_lookups:
                    payload["lookup_status"] = status
                    if info:
                        payload["lookup_info"] = info

            if lookup_id:
                payload["lookup_id"] = lookup_id

    if args.update_stock and update_url:
        log_path = Path("gen/update_stock.log")
        for line in lines:
            payload = line.get("payload", {})
            product_id = payload.get("lookup_id") or payload.get("reference")
            qty = payload.get("quantity")
            if qty is None:
                continue
            delta = -abs(qty) if args.template_type == "facture" else abs(qty)
            reason = invoice_number or args.update_reason
            initial_stock = payload.get("initial_stock")
            new_stock = None
            if initial_stock is not None:
                try:
                    new_stock = float(initial_stock) + delta
                except Exception:
                    new_stock = None
            if new_stock is None:
                new_stock = delta  # fallback: ancien comportement

            success = patch_stock(update_url, str(product_id), new_stock, reason, headers)
            if success:
                payload["stock_update"] = {"delta": delta, "new_stock": new_stock, "status": "patched"}
            else:
                payload["stock_update"] = {"delta": delta, "new_stock": new_stock, "status": "failed"}
            if invoice_number:
                payload["invoice_number"] = invoice_number
            log_stock_event(
                log_path,
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "reference": payload.get("reference"),
                    "product_id": product_id,
                    "lookup_id": payload.get("lookup_id"),
                    "delta": delta,
                    "reason": reason,
                    "invoice_number": invoice_number,
                    "initial_stock": initial_stock,
                    "new_stock": new_stock,
                    "status": payload["stock_update"]["status"],
                },
            )

    if args.csv:
        write_csv(lines, args.csv)
    else:
        write_output(lines, None, args.ndjson)


if __name__ == "__main__":
    main()
