const { app } = require('@azure/functions');
const sql = require('mssql');

const config = {
    user: process.env.SQL_USER,
    password: process.env.SQL_PASSWORD,
    server: '10.0.1.4',
    database: 'LatidataSQL',
    options: {
        instanceName: 'SQLexpress',
        trustServerCertificate: true,
        encrypt: false
    }
};

async function getPool() {
    if (!sql.pool || !sql.pool.connected) {
        await sql.connect(config);
    }
    return sql;
}

// ---------------------------------------------------------------------------
// GET /api/getJobs
// Query params: job_number, status, client, limit
// ---------------------------------------------------------------------------
app.http('getJobs', {
    methods: ['GET'],
    authLevel: 'anonymous',
    handler: async (request, context) => {
        try {
            const params = new URL(request.url).searchParams;
            const jobNumber = params.get('job_number') || null;
            const status    = params.get('status')     || null;
            const client    = params.get('client')     || null;
            const limit     = Math.min(parseInt(params.get('limit') || '200'), 500);

            await getPool();
            const req = new sql.Request();

            let query = `
                SELECT TOP (@limit)
                    [Job Number],
                    [Job Date],
                    [Job Description],
                    txtJobName,
                    JobType,
                    Client,
                    Locality,
                    [Work Status],
                    [Instructing Person],
                    dteJobUserField25 AS provisioned_date
                FROM dbo.tblJobs
                WHERE 1=1
            `;

            req.input('limit', sql.Int, limit);

            if (jobNumber) {
                query += ' AND [Job Number] LIKE @jobNumber';
                req.input('jobNumber', sql.NVarChar, `${jobNumber}%`);
            }
            if (status) {
                query += ' AND [Work Status] = @status';
                req.input('status', sql.NVarChar, status);
            }
            if (client) {
                query += ' AND Client = @client';
                req.input('client', sql.NVarChar, client);
            }

            query += ' ORDER BY [Job Date] DESC';

            const result = await req.query(query);

            return {
                status: 200,
                jsonBody: result.recordset
            };
        } catch (err) {
            context.error('getJobs error:', err);
            return { status: 500, body: err.message };
        }
    }
});

// ---------------------------------------------------------------------------
// GET /api/getStatuses
// Returns distinct Work Status values
// ---------------------------------------------------------------------------
app.http('getStatuses', {
    methods: ['GET'],
    authLevel: 'anonymous',
    handler: async (request, context) => {
        try {
            await getPool();
            const result = await sql.query(`
                SELECT DISTINCT [Work Status]
                FROM dbo.tblJobs
                WHERE [Work Status] IS NOT NULL
                ORDER BY [Work Status]
            `);
            return {
                status: 200,
                jsonBody: result.recordset.map(r => r['Work Status'])
            };
        } catch (err) {
            context.error('getStatuses error:', err);
            return { status: 500, body: err.message };
        }
    }
});

// ---------------------------------------------------------------------------
// POST /api/markProvisioned
// Body: { job_number: "260174" }
// Sets tblJobs.dteJobUserField25 = GETDATE() for the given job.
// ---------------------------------------------------------------------------
app.http('markProvisioned', {
    methods: ['POST'],
    authLevel: 'anonymous',
    handler: async (request, context) => {
        try {
            const body      = await request.json();
            const jobNumber = (body && body.job_number) ? String(body.job_number).trim() : null;

            if (!jobNumber) {
                return { status: 400, body: 'job_number is required' };
            }

            await getPool();
            const req = new sql.Request();
            req.input('jobNumber', sql.NVarChar, jobNumber);

            const result = await req.query(`
                UPDATE dbo.tblJobs
                SET dteJobUserField25 = GETDATE()
                WHERE [Job Number] = @jobNumber
            `);

            if (result.rowsAffected[0] === 0) {
                return { status: 404, body: `Job '${jobNumber}' not found` };
            }

            return {
                status: 200,
                jsonBody: { job_number: jobNumber, provisioned: true }
            };
        } catch (err) {
            context.error('markProvisioned error:', err);
            return { status: 500, body: err.message };
        }
    }
});

// ---------------------------------------------------------------------------
// GET /api/getClients
// Query params: client_code
// ---------------------------------------------------------------------------
app.http('getClients', {
    methods: ['GET'],
    authLevel: 'anonymous',
    handler: async (request, context) => {
        try {
            const params     = new URL(request.url).searchParams;
            const clientCode = params.get('client_code') || null;

            await getPool();
            const req = new sql.Request();

            let query = 'SELECT [Client Code], [Company Name] FROM dbo.tblClient WHERE 1=1';

            if (clientCode) {
                query += ' AND [Client Code] = @clientCode';
                req.input('clientCode', sql.NVarChar, clientCode);
            }

            query += ' ORDER BY [Client Code]';

            const result = await req.query(query);
            return {
                status: 200,
                jsonBody: result.recordset.map(r => ({
                    client_code:  r['Client Code'],
                    company_name: r['Company Name']
                }))
            };
        } catch (err) {
            context.error('getClients error:', err);
            return { status: 500, body: err.message };
        }
    }
});
