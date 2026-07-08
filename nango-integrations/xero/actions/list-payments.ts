import { createAction } from 'nango';
import * as z from 'zod';

/**
 * list-payments — one page of Xero Payments. Each payment carries `IsReconciled`
 * (the bank-matched flag) and the `Invoice` it settles, used to mark which
 * invoices/bills are bank-reconciled for the duplicate risk signal. Paginated
 * like invoices — the caller loops pages until an empty one.
 */
export default createAction({
    description: 'One page of Xero payments (PaymentID, invoice, amount, date, status, reconciled).',
    version: '1.0.0',
    input: z.object({
        tenantId: z.string().optional(),
        page: z.number().optional(),
        where: z.string().optional(),
        modifiedSince: z.string().optional(),
    }),
    output: z.object({
        count: z.number(),
        payments: z.array(z.record(z.string(), z.unknown())),
    }),
    exec: async (nango, input) => {
        const meta = (await nango.getMetadata()) as { tenant_id?: string } | null;
        const tenantId = input.tenantId ?? meta?.tenant_id;
        const headers: Record<string, string> = {};
        if (tenantId) headers['xero-tenant-id'] = tenantId;
        if (input.modifiedSince) headers['If-Modified-Since'] = input.modifiedSince;

        const res = await nango.get({
            endpoint: 'api.xro/2.0/Payments',
            params: { page: input.page ?? 1, ...(input.where ? { where: input.where } : {}) },
            headers,
        });

        const payments = (res.data?.Payments ?? []) as Array<Record<string, unknown>>;
        return { count: payments.length, payments };
    },
});
