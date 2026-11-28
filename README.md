# Crest Vault Africa
### Non-Custodial Cross-Border Trade Clearing, Anti-Counterfeit Passports & Regulatory Tax OS on the XRP Ledger

**Live Prototype:** [https://onrender.com](https://onrender.com)  
**Target Markets:** Uganda (UGX), Kenya (KES), Nigeria (NGN), South Africa (ZAR), Rwanda (RWF), Ghana (GHS), UAE (AED)

---

## 1. Executive Summary
**Crest Vault Africa** solves three critical bottlenecks in East African and cross-border emerging-market trade without exposing everyday merchants, factories, or truckers to blockchain complexity:

1. **Eliminating Cross-Border Supplier Fraud & SWIFT Delays:** Importers (e.g., Kikuubo/Kampala traders) and Agro-Exporters (coffee, cocoa, pharma) settle in familiar USD and local fiat (`UGX`/`KES`) via licensed regional off-ramps, while the **XRP Ledger (`RLUSD` / `USDC` + Native Escrow)** silently locks and releases funds across **3-Bucket Milestone Tranches (`M1` Deposit, `RECTS` Freight Carrier, `M2` Final Delivery)**.
2. **Automating Tax & Customs Compliance (URA EFRIS, ASYCUDA, UNBS, NDA, UCDA):** Every trade lock, milestone disbursement, and export invoice locks the daily exchange rate, generates EFRIS Fiscal Document Numbers (`FDN`), and outputs **scannable QR-stamped official compliance packs** linked directly to verifiable XRPL transaction hashes—preventing arbitrary customs valuation uplifts.
3. **Serialized Anti-Counterfeit QR Passports & SME Credit Underwriting:** Manufacturers mint tamper-evident serialized QR codes per batch (`UNBS` / `NDA` / `UCDA`). Consumers and border inspectors verify authenticity with any smartphone camera (`VERIFIED GENUINE` vs. `COUNTERFEIT CLONE ALERT` with district tracking), while retail shops build tamper-proof trading ledgers to unlock bank/SACCO credit.

---

## 2. Live Verified XRPL Testnet Proof
Our live deployment connects directly to the **XRPL Testnet (`https://rippletest.net`)** and produces verifiable on-ledger transactions with decoded off-ramp settlement memos (`OFFRAMP_SETTLEMENT`, `OFFRAMP_M1`, `OFFRAMP_FREIGHT`, `OFFRAMP_M2`):

* **Live Escrow Lock (`EscrowCreate` — `tesSUCCESS`):**  
  `8A4981CD89A79FD0D5436D49A9B5C0DDD5765EE82D092F601D66F2C2314453D9`  
  [View on XRPL Testnet Explorer](https://xrpl.org)
* **Live Milestone Stablecoin Payout (`Payment` `7,000.00 RLUSD` — `tesSUCCESS`):**  
  `5FDB7A24EBC55BF86D0D25BD99E9FA4E252BC93E1EBD869DE5248BC9B2D399C0`  
  [View on XRPL Testnet Explorer](https://xrpl.org)

---

## 3. Non-Custodial Architecture & Custody Disclosure
Crest Vault Africa is engineered strictly as a **non-custodial trade compliance and clearing orchestrator**:

* **Zero Fiat Custody:** Customer fiat (`UGX`/`KES`/`NGN`) never enters Crest Vault Africa's bank accounts. Payments route directly between buyers and licensed regional Payment Service Providers / Off-Ramps (Yellow Card, Kotani Pay, Fonbnk, Pesapal) configured via the Level 6 API Socket Manager (`/api/admin/config`).
* **Zero Key Custody in Production:** Once live off-ramp API credentials are connected in production, all liquidity conversion and XRPL transaction signing occur exclusively inside the licensed off-ramp partners' regulated vaults. Crest Vault Africa executes read-only on-ledger receipt verification (`Tx`), generates QR-stamped regulatory documents, and collects flat SaaS/escrow software fees (`$15`–`$199/mo`, `$35/escrow`).
* **Live Sandbox Relayer Mode:** In this public Testnet MVP, an isolated faucet-funded sandbox relayer (`get_3_actors` in `main.py` using `AccountSetAsfFlag.ASF_DEFAULT_RIPPLE`) simulates the off-ramp liquidity partner so sandbox users and partners can execute end-to-end 3-bucket escrow locks and `RLUSD` milestone payouts in one click without installing browser wallet extensions.

---

## 4. Role-Tailored Multi-User Workspace
Signing in dynamically adapts the workspace, forms, and printable QR documents to each ecosystem participant:

| Role | Tailored Workspace & Capabilities |
| :--- | :--- |
| **Importer (`IMPORTER`)** | Locks 3-bucket split escrows (`M1` / `Freight` / `M2`), pays URA ASYCUDA PRNs, releases milestone tranches directly to overseas supplier bank rails, and downloads QR-stamped Customs Valuation Packs. |
| **Exporter / Factory (`EXPORTER`)** | Issues zero-rated EFRIS export invoices with Destination Tags & AML flags, attaches UCDA/UNBS permits & Bills of Lading, and routes buyer settlements automatically via off-ramp to local commercial banks (no withdrawal step). |
| **Freight Trucker (`CARRIER`)** | Views open cross-border loads, submits bids with Truck Plate, RECTS Electronic Seal ID, and Mobile Money/Bank payout details, and tracks `APPROVED` / `NOT_SELECTED` status. |
| **Domestic Factory / Wholesaler (`WHOLESALER`)** | Issues local `UGX` EFRIS invoices with instant Factory Gate Dispatch Passes, mints serialized anti-counterfeit batch QR codes, and tracks 14-day retailer credit. |
| **Retail Shop / Pharmacy (`RETAILER`)** | Scans incoming cartons to reject counterfeits, logs daily cash/MoMo sales and allowable expenses, and generates a QR-stamped **6-Month Bank-Verified Credit Passport**. |
| **Regulator (`REGULATOR` — URA / UNBS / NDA)** | Read-only surveillance dashboard for cross-border customs valuations, EFRIS export ledgers, active permits, and live district-level counterfeit clone alerts. |
| **External CPA (`AUDITOR`)** | Read-only reconciled local-currency tax ledger with locked daily FX rates for 1-click VAT & Income Tax filing. |
| **Platform Owner (`SUPER_ADMIN`)** | Configures SaaS pricing tiers and live API sockets for Off-Ramps, Local Gateways, URA EFRIS, ASYCUDA/RECTS, UNBS/NDA, and FX Oracles. |

---

## 5. Open-Source Ecosystem Contribution (MIT License)
As part of our open-source ecosystem contribution, we are extracting and maintaining an open-source Python module—**`xrpl-offramp-trade-escrow`** (MIT Licensed)—providing:
1. **Standardized 3-Bucket Trade Escrow & Off-Ramp Memo Builder:** Deterministic hex-encoded memo schemas (`OFFRAMP_SETTLEMENT`, `OFFRAMP_M1`, `OFFRAMP_FREIGHT`, `OFFRAMP_M2`) for cross-border B2B bank routing.
2. **Non-Custodial On-Chain Receipt Verifier:** Read-only `tesSUCCESS` and memo validator for payment orchestrators.
3. **Custom `DefaultRipple` Testnet Issuer Harness:** Automated Testnet issuer + `TrustSet` setup utility for stablecoin integration testing.

---

## 6. Local Setup & Deployment
```bash
git clone https://github.com/hermann781227/crestvault-africa.git
cd crestvault-africa
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 3000
```

### Environment Variables
* `PORT`: Server port (default `3000`)
* `APP_URL`: Public base URL for scannable QR verification links (e.g., `https://onrender.com`)
* `ADMIN_MASTER_PIN`: Master PIN for Level 6 Owner Console
* `XRPL_MASTER_SEED`: Testnet sandbox relayer seed (removed in production when live off-ramp API keys are active)
