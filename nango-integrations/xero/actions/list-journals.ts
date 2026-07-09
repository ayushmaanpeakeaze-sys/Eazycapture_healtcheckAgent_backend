import { createAction } from 'nango';
import * as z from 'zod';

/**
 * list-journals — one page (100) of Xero Journals: the raw general-ledger
 * postings WITH their JournalLines (AccountCode, AccountType, Description,
 * NetAmount, GrossAmount, TaxAmount).
 *
 * Why this exists: Journals are the accounting truth — every posting hits the
 * GL, INCLUDING manual journal entries that never appear as an invoice or bank
 * transaction. The Revenue-vs-Capital review needs them so a capital item booked
 * straight to the ledger isn't missed.
 *
 * Pagination: Xero /Journals is OFFSET-based (by JournalNumber), not page-based,
 * and returns 100 per call. To keep the caller uniform with every other list
 * action this takes a 1-based `page` and converts it to offset = (page-1)*100
 * (Xero JournalNumbers are sequential, so pages are contiguous). Needs the
 * `accounting.journals.read` scope on the connection.
 */
export default createAction({
    description: 'One page (100) of Xero journals with lines — raw GL postings incl. manual journals.',
    version: '1.0.0',
    input: z.object({
        tenantId: z.string().optional(),
        page: z.number().int().positive().optional(),
        modifiedSince: z.string().optional(),
    }),
    output: z.object({
        page: z.number(),
        count: z.number(),
        journals: z.array(z.record(z.string(), z.unknown())),
    }),
    exec: async (nango, input) => {
        const page = input.page ?? 1;
        const offset = (page - 1) * 100;   // Xero Journals paginate by JournalNumber offset

        const meta = (await nango.getMetadata()) as { tenant_id?: string } | null;
        const tenantId = input.tenantId ?? meta?.tenant_id;
        const headers: Record<string, string> = {};
        if (tenantId) headers['xero-tenant-id'] = tenantId;
        if (input.modifiedSince) headers['If-Modified-Since'] = input.modifiedSince;

        const res = await nango.get({
            endpoint: 'api.xro/2.0/Journals',
            params: { offset },
            headers,
        });

        const journals = (res.data?.Journals ?? []) as Array<Record<string, unknown>>;
        return { page, count: journals.length, journals };
    },
});
