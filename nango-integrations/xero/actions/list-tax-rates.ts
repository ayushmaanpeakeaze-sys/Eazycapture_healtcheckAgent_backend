import { createAction } from 'nango';
import * as z from 'zod';

/**
 * list-tax-rates — every Xero tax rate (TaxType, Name, Status, and the component
 * rates). Feeds the tax checks (sales/purchase tax missing, unexpected tax code,
 * multi-tax supplier). The set is small and Xero does not paginate it, so this is
 * a single call returning the raw `TaxRates` array.
 */
export default createAction({
    description: 'Full Xero tax rates (TaxType, name, status, component rates).',
    version: '1.0.0',
    input: z.object({
        tenantId: z.string().optional(),
        where: z.string().optional(),
        modifiedSince: z.string().optional(),
    }),
    output: z.object({
        count: z.number(),
        taxRates: z.array(z.record(z.string(), z.unknown())),
    }),
    exec: async (nango, input) => {
        const meta = (await nango.getMetadata()) as { tenant_id?: string } | null;
        const tenantId = input.tenantId ?? meta?.tenant_id;
        const headers: Record<string, string> = {};
        if (tenantId) headers['xero-tenant-id'] = tenantId;
        if (input.modifiedSince) headers['If-Modified-Since'] = input.modifiedSince;

        const res = await nango.get({
            endpoint: 'api.xro/2.0/TaxRates',
            params: { ...(input.where ? { where: input.where } : {}) },
            headers,
        });

        const taxRates = (res.data?.TaxRates ?? []) as Array<Record<string, unknown>>;
        return { count: taxRates.length, taxRates };
    },
});
