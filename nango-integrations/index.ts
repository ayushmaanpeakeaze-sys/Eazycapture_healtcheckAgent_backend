// Nango Functions — deployment entry point.
//
// EVERY function file MUST be imported here, or `nango deploy` will NOT include
// it. ESM resolution → import .ts files with a .js extension.

// --- Xero: reads (full line items / fields — pre-built strip them) ---
import './xero/actions/list-invoices-full.js';          // invoices + bills + line items
import './xero/actions/list-bank-transactions-full.js'; // Money In/Out + lines + IsReconciled
import './xero/actions/list-credit-notes-full.js';      // credit notes + lines + RemainingCredit
import './xero/actions/list-contacts-full.js';          // contacts + defaults + email + status
import './xero/actions/list-accounts-full.js';          // chart of accounts (type/taxtype)
import './xero/actions/list-tax-rates.js';              // tax rates (sales/purchase tax + drift checks)
import './xero/actions/list-payments.js';               // payments (IsReconciled + settled invoice)
import './xero/actions/list-organisation.js';           // organisation (base currency / period)
import './xero/actions/get-trial-balance.js';           // trial balance report (Balance in Xero)
import './xero/syncs/invoices-full.js';

// --- Xero: writes (the "buttons" that fix flagged issues) ---
import './xero/actions/update-invoice.js';              // Approve/Delete + recode line/tax
import './xero/actions/update-contact.js';              // defaults / archive
import './xero/actions/create-credit-note.js';          // old-unpaid write-off
import './xero/actions/revoke-connection.js';           // per-org disconnect (permanently forget)
