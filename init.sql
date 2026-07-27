CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    client_name TEXT NOT NULL,
    addresses JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    cms_order_id TEXT,
    wms_package_id TEXT,
    ros_route_id TEXT,
    failed_step TEXT,
    failure_reason TEXT,
    delivery_status TEXT,
    delivery_reason TEXT,
    delivered_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);
