# pdf2json

Script Python qui extrait les lignes d'une facture PDF et produit un JSON pret a etre envoye en body d'une requete API.

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Utilisation

```bash
python invoice_parser.py chemin/vers/facture.pdf
```

Options utiles :

- `--ndjson` : un JSON par ligne (NDJSON) plutot qu'une liste

Le payload propose pour chaque ligne a la cle `payload` contient `description`, `quantity`, `unit_price` et `line_total` (si detectables). Toutes les colonnes brutes restent dans `columns` pour affiner ou mapper vers votre API.

### Exemple rapide avec le template fourni

```bash
python invoice_parser.py templates/facture.pdf --csv lignes.csv
```

### Lookup d'ID via un endpoint (optionnel)

Ajoute un appel HTTP pour chaque reference afin de recuperer un identifiant (par exemple un id produit). L'URL peut contenir `{reference}` qui sera remplacee.

```bash
python invoice_parser.py templates/facture.pdf --csv lignes.csv
```

La colonne `lookup_id` apparaîtra dans le CSV et dans le JSON si l'appel retourne un `id` (ou un `data.id`, ou le `id` du premier element d'une liste JSON).

#### Configuration par fichier

- Mets tes endpoints dans `config.json` (copie `config.example.json` et adapte `lookup_url` ou `lookup_products_url`/`products_url`, ainsi que `update_product_stock_url`). `config.example.json` est anonymise : n'y mets pas tes cles.
- Mets ta clé API dans `utils/api_key.txt` (elle sera envoyée en header `userApiKey`). Tu peux changer le chemin avec `--api-key-path`.

Appel minimal avec config et clé api déjà en place :

```bash
python invoice_parser.py templates/facture.pdf --csv lignes.csv
```

### Choix du template (facture ou avoir)

```bash
python invoice_parser.py templates/facture.pdf --template-type facture --csv lignes_facture.csv
python invoice_parser.py templates/avoir.pdf   --template-type avoir   --csv lignes_avoir.csv
```

### Mise à jour de stock (PATCH)

- Endpoint lu depuis `config.json` clé `update_product_stock_url` (exemple dans `config.example.json`).
- Corps de requête similaire à `utils/update_product.json` : `stock` envoyé est calculé à partir du stock initial du produit (si présent dans le cache) + delta (facture = -qty, avoir = +qty). Si le stock initial est inconnu, on retombe sur l’ancien comportement (envoi de delta). `update_reason` est renseigné automatiquement avec le numéro de facture/avoir extrait du PDF (par ex. `F25045691`, `AVID_25004993`); il est possible de l’écraser via `--update-reason` si besoin.

```bash
python invoice_parser.py templates/facture.pdf --template-type facture \
  --update-stock --update-reason "decr via pdf" --csv lignes.csv
```

Le script utilise l’`lookup_id` trouvé, ou la `reference` si pas d’ID, pour remplacer `{product_id}` dans l’URL. Le résultat du PATCH est indiqué dans `payload.stock_update`.
- Lorsqu'on passe `--update-stock`, un log NDJSON est écrit dans `gen/update_stock.log` (ts, référence, lookup_id, delta, raison, numéro de facture/avoir, statut).

### Cache produits (évite trop d'appels)

- Le script peut lister tous les produits et les mettre en cache local `.cache/products.json`.
- Utilise `--refresh-products` pour forcer un refetch, sinon le cache est relu.
- L'URL de listing vient de `config.json` (`products_url` ou `lookup_products_url` sans param).
- `--verbose-products` affiche les statuts/pages pour diagnostiquer.
