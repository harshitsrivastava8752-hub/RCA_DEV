CREATE TABLE IF NOT EXISTS orders (
  id SERIAL PRIMARY KEY,
  item TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO orders (item) VALUES ('widget'), ('gadget'), ('gizmo');
