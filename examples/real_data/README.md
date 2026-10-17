# Public real-data examples

`online_retail_sample.jsonl` contains 10 actual line items from the UCI Online Retail dataset. The sample keeps the source values for invoice, product, quantity, local invoice time, price, and country. It omits `CustomerID`. `source_row` is the original workbook row number and is the unique key for this line-item sample.

The invoice time has no timezone in the source. The example keeps it timezone-naive rather than assigning an assumed offset. The schema allows `customer_id` for a full local copy and marks it for run-scoped pseudonymization when `--anonymize` is used.

UCI citation: Daqing Chen (2015), *Online Retail*, UCI Machine Learning Repository, DOI [10.24432/C5BW33](https://doi.org/10.24432/C5BW33). The dataset is licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The original dataset page describes 541,909 transactions from a UK-based online retailer between December 2010 and December 2011.

Run the sample from the repository root:

```powershell
python main.py --input examples\real_data\online_retail_sample.jsonl --config examples\real_data\online_retail_schema.json --anonymize --output-dir real-data-output
```

The source dataset is an XLSX file, which AirLock DB does not ingest. This checked-in sample is already JSONL. Testing the full workbook requires converting it to JSONL first. That conversion does not add native XLSX support to AirLock DB.

## Real bank transactions with the built-in schema

[MoneyData](https://data.mendeley.com/datasets/dnxtg6n4rv/1) is an anonymized, real-world retail bank ledger published under CC BY 4.0. It has 6,567 rows and separate debit and credit amounts. Cite Robert Laramee et al., *MoneyVis: Open Bank Transaction Data for Visualization and Beyond*, Eurovis 2023, [DOI 10.2312/evs.20231052](https://doi.org/10.2312/evs.20231052), and the dataset [DOI 10.17632/dnxtg6n4rv.1](https://doi.org/10.17632/dnxtg6n4rv.1).

The workbook does not provide an account ID, currency, time of day, or status. `prepare_moneydata_jsonl.py` makes these explicit test-only choices: it uses a source-local account alias, derives a stable UUID from the source transaction number, sets currency to GBP for the UK dataset, marks historical statement entries `POSTED`, and writes date-only values as midnight UTC. The timestamp does not claim that the source recorded a time of day. It maps debit and credit direction from the populated amount column, uses the transaction description as `counterparty`, and uses the source category as `description`.

This optional converter requires `openpyxl` only in the data-preparation environment. It does not add a project or AirLock DB runtime dependency. Download the XLSX file from Mendeley Data, then convert and run it from the repository root:

```powershell
python -m pip install openpyxl
python examples\real_data\prepare_moneydata_jsonl.py C:\data\laramee26openBankTransactionData.xlsx C:\data\moneydata.jsonl
python main.py --input C:\data\moneydata.jsonl --anonymize --output-dir C:\data\moneydata-output
```

Use `--anonymize` because transaction descriptions are part of the input. In the acceptance run, 6,555 of 6,567 rows were accepted. The other 12 had numeric values in the source description column, so the strict target schema rejected them as type drift rather than converting them to text. The CLI warning and report tell reviewers that no automatic conversion was applied.
