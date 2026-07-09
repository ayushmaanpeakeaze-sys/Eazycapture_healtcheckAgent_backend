import { createAction } from 'nango';
import * as z from 'zod';

/**
 * list-assets — one page of the Xero Fixed Asset register (the `assets.xro/1.0`
 * API, a different base path from the accounting `api.xro/2.0`).
 *
 * The validation layer for the Revenue-vs-Capital review: an expense that reads
 * like a capital purchase should NOT be flagged if the item is already on the
 * fixed-asset register. Defaults to REGISTERED assets (the capitalised ones).
 * Needs the `assets.read` scope on the connection.
 *
 * Pagination: page + pageSize (the response carries a `pagination` block and the
 * rows under `items`). The caller loops pages until an empty one.
 */
export default createAction({
    description: 'One page of the Xero fixed-asset register (assetName, purchaseDate, purchasePrice, status).',
    version: '1.0.0',
    input: z.object({
        tenantId: z.string().optional(),
        page: z.number().int().positive().optional(),
        pageSize: z.number().int().positive().optional(),
        status: z.string().optional(),
    }),
    output: z.object({
        page: z.number(),
        count: z.number(),
        assets: z.array(z.record(z.string(), z.unknown())),
    }),
    exec: async (nango, input) => {
        const page = input.page ?? 1;
        const meta = (await nango.getMetadata()) as { tenant_id?: string } | null;
        const tenantId = input.tenantId ?? meta?.tenant_id;
        const headers: Record<string, string> = {};
        if (tenantId) headers['xero-tenant-id'] = tenantId;

        const res = await nango.get({
            endpoint: 'assets.xro/1.0/Assets',
            params: {
                status: input.status ?? 'REGISTERED',
                page,
                pageSize: input.pageSize ?? 100,
            },
            headers,
        });

        const assets = (res.data?.items ?? []) as Array<Record<string, unknown>>;
        return { page, count: assets.length, assets };
    },
});
