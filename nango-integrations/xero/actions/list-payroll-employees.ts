import { createAction } from 'nango';
import * as z from 'zod';

/**
 * list-payroll-employees — one page of Xero Payroll employees (the
 * `payroll.xro/2.0` API, a different base path from the accounting `api.xro/2.0`).
 *
 * The approved-staff list for the "Bank payments to people not in payroll" SOP:
 * a bank payee that is NOT in this list is flagged for client confirmation.
 * Needs the `payroll.employees.read` scope on the connection.
 */
export default createAction({
    description: 'One page of Xero Payroll employees (FirstName, LastName, EmployeeID, Status).',
    version: '1.0.0',
    input: z.object({
        tenantId: z.string().optional(),
        page: z.number().int().positive().optional(),
    }),
    output: z.object({
        page: z.number(),
        count: z.number(),
        employees: z.array(z.record(z.string(), z.unknown())),
    }),
    exec: async (nango, input) => {
        const page = input.page ?? 1;
        const meta = (await nango.getMetadata()) as { tenant_id?: string } | null;
        const tenantId = input.tenantId ?? meta?.tenant_id;
        const headers: Record<string, string> = {};
        if (tenantId) headers['xero-tenant-id'] = tenantId;

        const res = await nango.get({
            endpoint: 'payroll.xro/2.0/Employees',
            params: { page },
            headers,
        });

        const employees = (res.data?.Employees ?? []) as Array<Record<string, unknown>>;
        return { page, count: employees.length, employees };
    },
});
