import { createSync } from 'nango';
import * as z from 'zod';

/**
 * invoices-full — Xero invoices/bills WITH full line items, as a SYNC.
 *
 * The pre-built `invoices` sync strips LineItems / Reference / HasAttachments
 * (header-only), which breaks every line-level check (low-cost FA, capital,
 * unexpected account/tax, misallocated, tax-missing, multi-account supplier).
 * Syncs have no 2 MB per-call cap (records are stored), so we hit the same
 * `/Invoices` endpoint the proxy uses — line items intact — and persist every
 * field. Incremental on `UpdatedDateUTC` via `If-Modified-Since`.
 *
 * `journals.read` scope / GL workaround NOT needed — this is the plain
 * Accounting `/Invoices` read (scope already granted).
 *
 * NOT auto-run. The backend fetches invoices on-demand via the
 * `list-invoices-full` ACTION (SyncEngine → our DB), so this continuous sync was
 * redundant and burned an hourly run for records nothing consumed. Kept deployed
 * with `autoStart: false` as an on-demand fallback ONLY — trigger it manually
 * (Nango `triggerSync`) for an org so large that action pagination is too slow.
 */
const Invoice = z.object({ id: z.string() }).passthrough();

export default createSync({
    description: 'On-demand fallback: Xero invoices/bills with full line items. Backend normally uses the list-invoices-full action; trigger this only for very large orgs.',
    version: '1.0.0',
    frequency: 'every hour',
    autoStart: false,
    syncType: 'incremental',
    trackDeletes: false,
    endpoints: [{ method: 'GET', path: '/invoices-full' }],
    models: { Invoice },
    metadata: z.object({ tenant_id: z.string() }),
    exec: async (nango) => {
        const meta = (await nango.getMetadata()) as { tenant_id?: string } | null;
        const tenantId = meta?.tenant_id;

        const headers: Record<string, string> = {};
        if (tenantId) headers['xero-tenant-id'] = tenantId;
        // Incremental: only invoices changed since the last successful run.
        if (nango.lastSyncDate) headers['If-Modified-Since'] = nango.lastSyncDate.toUTCString();

        let page = 1;
        while (true) {
            const res = await nango.get({
                endpoint: 'api.xro/2.0/Invoices',
                params: { page },
                headers,
            });
            const invoices = (res.data?.Invoices ?? []) as Array<Record<string, unknown>>;
            if (invoices.length === 0) break;

            // Key each record by its Xero InvoiceID; keep every field (line items, etc.).
            await nango.batchSave(
                invoices.map((inv) => ({ ...inv, id: String(inv['InvoiceID']) })),
                'Invoice',
            );
            page++;
        }
    },
});
