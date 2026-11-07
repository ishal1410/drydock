-- DryDock synthetic demo DB: a fake B2B SaaS billing system. All data is generated, nothing real.
CREATE TABLE customers (
  id serial PRIMARY KEY,
  name text NOT NULL,
  email text NOT NULL,
  status text NOT NULL CHECK (status IN ('active','churned'))
);
CREATE TABLE accounts (
  id serial PRIMARY KEY,
  customer_id int NOT NULL REFERENCES customers(id),
  code text NOT NULL UNIQUE,
  status text NOT NULL CHECK (status IN ('active','closed')),
  balance_cents bigint NOT NULL,
  opened_on date NOT NULL,
  usage_cents_oct bigint NOT NULL DEFAULT 0   -- metered usage charge for October 2026
);
CREATE TABLE subscriptions (
  id serial PRIMARY KEY,
  account_id int NOT NULL REFERENCES accounts(id),
  plan text NOT NULL CHECK (plan IN ('starter','pro','enterprise')),
  status text NOT NULL CHECK (status IN ('active','cancelled')),  -- one spelling across schema and data
  monthly_cents bigint NOT NULL
);
CREATE TABLE invoices (
  id serial PRIMARY KEY,
  number text NOT NULL UNIQUE,
  account_id int NOT NULL REFERENCES accounts(id),
  subscription_id int REFERENCES subscriptions(id),
  amount_cents bigint NOT NULL,
  paid_cents bigint NOT NULL DEFAULT 0,
  issued_on date NOT NULL,
  due_on date NOT NULL,
  status text NOT NULL CHECK (status IN ('open','paid','written_off','void'))
);
CREATE TABLE payments (
  id serial PRIMARY KEY,
  invoice_id int NOT NULL REFERENCES invoices(id),
  amount_cents bigint NOT NULL,
  paid_on date NOT NULL,
  method text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('charge','refund')),
  refund_of int REFERENCES payments(id),      -- the charge a refund reverses; NULL for charges
  CHECK ((kind = 'refund') = (refund_of IS NOT NULL))
);
CREATE TABLE payment_plans (
  id serial PRIMARY KEY,
  invoice_id int NOT NULL REFERENCES invoices(id),
  installments int NOT NULL,
  status text NOT NULL CHECK (status IN ('active','completed','cancelled'))
);
CREATE TABLE credits (
  id serial PRIMARY KEY,
  account_id int NOT NULL REFERENCES accounts(id),
  amount_cents bigint NOT NULL,
  reason text NOT NULL,
  created_on date NOT NULL
);
