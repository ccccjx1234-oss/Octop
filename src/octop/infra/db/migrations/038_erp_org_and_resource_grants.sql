-- Schema v38: Huihuo ERP organization directory projection and unified resource grants.
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS erp_departments (
  company_id INTEGER NOT NULL,
  org_id     INTEGER NOT NULL,
  parent_id  INTEGER NOT NULL DEFAULT 1,
  name       TEXT NOT NULL,
  org_code   TEXT NOT NULL DEFAULT '',
  sort       INTEGER NOT NULL DEFAULT 0,
  status     INTEGER NOT NULL DEFAULT 1,
  synced_at  INTEGER NOT NULL,
  PRIMARY KEY (company_id, org_id)
);
CREATE INDEX IF NOT EXISTS idx_erp_departments_parent
  ON erp_departments(company_id, parent_id);

CREATE TABLE IF NOT EXISTS erp_department_closure (
  company_id        INTEGER NOT NULL,
  ancestor_org_id   INTEGER NOT NULL,
  descendant_org_id INTEGER NOT NULL,
  depth             INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (company_id, ancestor_org_id, descendant_org_id)
);
CREATE INDEX IF NOT EXISTS idx_erp_dept_closure_desc
  ON erp_department_closure(company_id, descendant_org_id);

CREATE TABLE IF NOT EXISTS erp_directory_employees (
  company_id      INTEGER NOT NULL,
  employee_id     INTEGER NOT NULL,
  account_user_id INTEGER,
  user_id         INTEGER REFERENCES users(id) ON DELETE SET NULL,
  nick_name       TEXT NOT NULL DEFAULT '',
  phone           TEXT NOT NULL DEFAULT '',
  employee_no     TEXT NOT NULL DEFAULT '',
  status          INTEGER NOT NULL DEFAULT 1,
  is_admin        INTEGER NOT NULL DEFAULT 0,
  synced_at       INTEGER NOT NULL,
  PRIMARY KEY (company_id, employee_id)
);
CREATE INDEX IF NOT EXISTS idx_erp_dir_emp_user
  ON erp_directory_employees(user_id);
CREATE INDEX IF NOT EXISTS idx_erp_dir_emp_company_status
  ON erp_directory_employees(company_id, status);

CREATE TABLE IF NOT EXISTS erp_employee_departments (
  company_id    INTEGER NOT NULL,
  employee_id   INTEGER NOT NULL,
  org_id        INTEGER NOT NULL,
  is_dept_admin INTEGER NOT NULL DEFAULT 0,
  synced_at     INTEGER NOT NULL,
  PRIMARY KEY (company_id, employee_id, org_id)
);
CREATE INDEX IF NOT EXISTS idx_erp_emp_dept_org
  ON erp_employee_departments(company_id, org_id);

CREATE TABLE IF NOT EXISTS resource_scope_policy (
  resource_type   TEXT NOT NULL,
  resource_key    TEXT NOT NULL,
  company_id      INTEGER NOT NULL DEFAULT 0,
  visibility_mode TEXT NOT NULL CHECK (visibility_mode IN ('owner', 'all', 'company', 'scoped')),
  updated_by      INTEGER,
  updated_at      INTEGER NOT NULL,
  PRIMARY KEY (resource_type, resource_key)
);

CREATE TABLE IF NOT EXISTS resource_subject_grant (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id       INTEGER NOT NULL DEFAULT 0,
  resource_type    TEXT NOT NULL,
  resource_key     TEXT NOT NULL,
  subject_type     TEXT NOT NULL CHECK (subject_type IN ('department', 'user', 'erp_employee', 'user_group')),
  subject_id       TEXT NOT NULL,
  include_children INTEGER NOT NULL DEFAULT 1,
  effect           TEXT NOT NULL DEFAULT 'allow' CHECK (effect IN ('allow', 'deny')),
  permission_level TEXT NOT NULL DEFAULT 'use' CHECK (permission_level IN ('use', 'manage')),
  granted_by       INTEGER,
  created_at       INTEGER NOT NULL,
  UNIQUE (resource_type, resource_key, subject_type, subject_id, effect)
);
CREATE INDEX IF NOT EXISTS idx_res_subject_grant_resource
  ON resource_subject_grant(resource_type, resource_key);
CREATE INDEX IF NOT EXISTS idx_res_subject_grant_subject
  ON resource_subject_grant(subject_type, subject_id);

UPDATE _schema_version SET version = 38;
COMMIT;
