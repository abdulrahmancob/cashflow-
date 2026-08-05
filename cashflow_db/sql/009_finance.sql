-- Finance Phase 3 stubs
CREATE TABLE IF NOT EXISTS finance.master_budget (
    budget_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    fiscal_year int NOT NULL,
    fiscal_month int NOT NULL CHECK (fiscal_month BETWEEN 1 AND 12),
    facility_id uuid REFERENCES ref.facility (facility_id),
    department text,
    revenue_target numeric(14, 2),
    expense_budget numeric(14, 2)
);

CREATE TABLE IF NOT EXISTS finance.cogs_entry (
    cogs_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    facility_id uuid REFERENCES ref.facility (facility_id),
    period_date date NOT NULL,
    component text NOT NULL
        CHECK (component IN ('labor', 'supplies', 'direct_utilities', 'documentation')),
    amount numeric(14, 2),
    allocation_split text
);

CREATE TABLE IF NOT EXISTS finance.department_scorecard (
    scorecard_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    department text NOT NULL,
    period_date date NOT NULL,
    budget_adherence numeric(8, 4),
    kpi_score numeric(8, 4),
    utilization_score numeric(8, 4),
    combined_score numeric(8, 4)
);
