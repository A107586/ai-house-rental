-- 基于 AI 智能体的房屋租赁系统（大连智租） 建库脚本 · SQLite 3
-- 大连理工大学 实习实训 第 6 小组
--
-- **这个文件是生成的，不要手改。** 改了也会被下一次生成覆盖，
-- 而且会和真正在跑的结构分叉。
--   源：server/app/models.py（SQLAlchemy 模型）
--   生成：python scripts/gen_db_sql.py
--   校验：python scripts/gen_db_sql.py --check（测试套件里也跑这一条）
--
-- 共 36 张表、388 个字段。建表顺序是依赖拓扑序，被引用的表先建。
--
-- 列默认值有意不写进 DDL：模型里的 default 是 Python 侧的，由 ORM 插入时填。
-- 写进来就等于多一份由数据库执行的语义，两边不一致时谁说了算会说不清。

-- SQLite 默认不校验外键，必须每个连接都打开一次。
-- 应用侧在 db.py 的 connect 事件里做了同样的事。
PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS "refunds";
DROP TABLE IF EXISTS "repair_item_usages";
DROP TABLE IF EXISTS "payments";
DROP TABLE IF EXISTS "viewing_records";
DROP TABLE IF EXISTS "reviews";
DROP TABLE IF EXISTS "repair_records";
DROP TABLE IF EXISTS "repair_images";
DROP TABLE IF EXISTS "chat_lines";
DROP TABLE IF EXISTS "bills";
DROP TABLE IF EXISTS "agent_commissions";
DROP TABLE IF EXISTS "repairs";
DROP TABLE IF EXISTS "house_reports";
DROP TABLE IF EXISTS "house_images";
DROP TABLE IF EXISTS "house_delegations";
DROP TABLE IF EXISTS "favorites";
DROP TABLE IF EXISTS "conversations";
DROP TABLE IF EXISTS "contracts";
DROP TABLE IF EXISTS "browsing_history";
DROP TABLE IF EXISTS "appointments";
DROP TABLE IF EXISTS "support_logs";
DROP TABLE IF EXISTS "support_handoffs";
DROP TABLE IF EXISTS "saved_searches";
DROP TABLE IF EXISTS "role_requests";
DROP TABLE IF EXISTS "password_resets";
DROP TABLE IF EXISTS "operation_logs";
DROP TABLE IF EXISTS "notices";
DROP TABLE IF EXISTS "messages";
DROP TABLE IF EXISTS "houses";
DROP TABLE IF EXISTS "content_flags";
DROP TABLE IF EXISTS "chat_messages";
DROP TABLE IF EXISTS "agent_profiles";
DROP TABLE IF EXISTS "users";
DROP TABLE IF EXISTS "system_configs";
DROP TABLE IF EXISTS "repair_workers";
DROP TABLE IF EXISTS "repair_items";
DROP TABLE IF EXISTS "faqs";

CREATE TABLE faqs (
	id INTEGER NOT NULL, 
	code VARCHAR(60) NOT NULL, 
	question VARCHAR(200) NOT NULL, 
	answer TEXT NOT NULL, 
	category VARCHAR(30) NOT NULL, 
	aliases VARCHAR(255) NOT NULL, 
	is_published BOOLEAN NOT NULL, 
	sort_order INTEGER NOT NULL, 
	hit_count INTEGER NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_faqs_category ON faqs (category);

CREATE UNIQUE INDEX ix_faqs_code ON faqs (code);

CREATE INDEX ix_faqs_is_published ON faqs (is_published);

CREATE TABLE repair_items (
	id INTEGER NOT NULL, 
	name VARCHAR(50) NOT NULL, 
	unit VARCHAR(10) NOT NULL, 
	unit_price NUMERIC(10, 2) NOT NULL, 
	stock INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE repair_workers (
	id INTEGER NOT NULL, 
	name VARCHAR(50) NOT NULL, 
	phone VARCHAR(20) NOT NULL, 
	skill VARCHAR(30) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE system_configs (
	id INTEGER NOT NULL, 
	"key" VARCHAR(60) NOT NULL, 
	value VARCHAR(255) NOT NULL, 
	value_type VARCHAR(10) NOT NULL, 
	label VARCHAR(60) NOT NULL, 
	description VARCHAR(200) NOT NULL, 
	group_name VARCHAR(30) NOT NULL, 
	is_public BOOLEAN NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE ("key")
);

CREATE TABLE users (
	id INTEGER NOT NULL, 
	username VARCHAR(50) NOT NULL, 
	password_hash VARCHAR(255) NOT NULL, 
	real_name VARCHAR(50) NOT NULL, 
	phone VARCHAR(20) NOT NULL, 
	email VARCHAR(100) NOT NULL, 
	token_valid_after DATETIME, 
	id_card_hash VARCHAR(64) NOT NULL, 
	id_card_masked VARCHAR(32) NOT NULL, 
	role VARCHAR(20) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	accept_contact BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (username)
);

CREATE INDEX ix_users_id_card_hash ON users (id_card_hash);

CREATE TABLE agent_profiles (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	license_status VARCHAR(20) NOT NULL, 
	license_no VARCHAR(40) NOT NULL, 
	license_images TEXT NOT NULL, 
	reviewed_by INTEGER, 
	reviewed_at DATETIME, 
	reject_reason VARCHAR(200) NOT NULL, 
	service_districts VARCHAR(120) NOT NULL, 
	intro VARCHAR(300) NOT NULL, 
	years INTEGER NOT NULL, 
	accepting BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(reviewed_by) REFERENCES users (id)
);

CREATE INDEX ix_agent_profiles_license_status ON agent_profiles (license_status);

CREATE UNIQUE INDEX ix_agent_profiles_user_id ON agent_profiles (user_id);

CREATE TABLE chat_messages (
	id INTEGER NOT NULL, 
	session_id VARCHAR(64) NOT NULL, 
	user_id INTEGER, 
	role VARCHAR(20) NOT NULL, 
	content TEXT NOT NULL, 
	intent VARCHAR(30) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE INDEX ix_chat_messages_session_id ON chat_messages (session_id);

CREATE TABLE content_flags (
	id INTEGER NOT NULL, 
	scene VARCHAR(20) NOT NULL, 
	target_id INTEGER NOT NULL, 
	target_key VARCHAR(64) NOT NULL, 
	author_id INTEGER, 
	author_name VARCHAR(50) NOT NULL, 
	excerpt VARCHAR(300) NOT NULL, 
	categories VARCHAR(120) NOT NULL, 
	reasons TEXT NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	handled_by VARCHAR(50) NOT NULL, 
	created_at DATETIME NOT NULL, 
	handled_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(author_id) REFERENCES users (id)
);

CREATE INDEX ix_content_flags_created_at ON content_flags (created_at);

CREATE INDEX ix_content_flags_scene ON content_flags (scene);

CREATE INDEX ix_content_flags_status ON content_flags (status);

CREATE TABLE houses (
	id INTEGER NOT NULL, 
	house_no VARCHAR(32) NOT NULL, 
	title VARCHAR(120) NOT NULL, 
	landlord_id INTEGER NOT NULL, 
	province VARCHAR(30) NOT NULL, 
	city VARCHAR(30) NOT NULL, 
	district VARCHAR(30) NOT NULL, 
	community VARCHAR(60) NOT NULL, 
	address VARCHAR(200) NOT NULL, 
	rent_price NUMERIC(10, 2) NOT NULL, 
	deposit NUMERIC(10, 2) NOT NULL, 
	area NUMERIC(10, 2) NOT NULL, 
	layout VARCHAR(20) NOT NULL, 
	rent_type VARCHAR(20) NOT NULL, 
	floor VARCHAR(20) NOT NULL, 
	orientation VARCHAR(20) NOT NULL, 
	description TEXT NOT NULL, 
	tags VARCHAR(200) NOT NULL, 
	cover_image VARCHAR(255) NOT NULL, 
	elevator BOOLEAN NOT NULL, 
	decoration VARCHAR(20) NOT NULL, 
	floor_no INTEGER, 
	floor_total INTEGER, 
	building VARCHAR(20) NOT NULL, 
	unit VARCHAR(20) NOT NULL, 
	heating VARCHAR(20) NOT NULL, 
	available_from DATE, 
	min_months INTEGER NOT NULL, 
	deposit_months INTEGER, 
	pay_months INTEGER, 
	service_fee NUMERIC(10, 2) NOT NULL, 
	business_area VARCHAR(30) NOT NULL, 
	lng FLOAT, 
	lat FLOAT, 
	location_source VARCHAR(20) NOT NULL, 
	geocoded_at DATETIME, 
	verify_status VARCHAR(20) NOT NULL, 
	verify_no VARCHAR(32) NOT NULL, 
	verified_at DATETIME, 
	verify_remark VARCHAR(200) NOT NULL, 
	maintained_at DATETIME NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	view_count INTEGER NOT NULL, 
	published_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (house_no), 
	FOREIGN KEY(landlord_id) REFERENCES users (id)
);

CREATE INDEX ix_houses_area ON houses (area);

CREATE INDEX ix_houses_available_from ON houses (available_from);

CREATE INDEX ix_houses_business_area ON houses (business_area);

CREATE INDEX ix_houses_decoration ON houses (decoration);

CREATE INDEX ix_houses_district ON houses (district);

CREATE INDEX ix_houses_elevator ON houses (elevator);

CREATE INDEX ix_houses_lat ON houses (lat);

CREATE INDEX ix_houses_layout ON houses (layout);

CREATE INDEX ix_houses_lng ON houses (lng);

CREATE INDEX ix_houses_rent_price ON houses (rent_price);

CREATE INDEX ix_houses_rent_type ON houses (rent_type);

CREATE INDEX ix_houses_status ON houses (status);

CREATE INDEX ix_houses_verify_no ON houses (verify_no);

CREATE INDEX ix_houses_verify_status ON houses (verify_status);

CREATE TABLE messages (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	type VARCHAR(20) NOT NULL, 
	title VARCHAR(120) NOT NULL, 
	content TEXT NOT NULL, 
	link_no VARCHAR(32) NOT NULL, 
	is_read BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE INDEX ix_messages_is_read ON messages (is_read);

CREATE INDEX ix_messages_type ON messages (type);

CREATE INDEX ix_messages_user_id ON messages (user_id);

CREATE TABLE notices (
	id INTEGER NOT NULL, 
	title VARCHAR(120) NOT NULL, 
	summary VARCHAR(255) NOT NULL, 
	content TEXT NOT NULL, 
	category VARCHAR(30) NOT NULL, 
	publisher_id INTEGER, 
	is_published BOOLEAN NOT NULL, 
	view_count INTEGER NOT NULL, 
	published_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(publisher_id) REFERENCES users (id)
);

CREATE INDEX ix_notices_category ON notices (category);

CREATE INDEX ix_notices_is_published ON notices (is_published);

CREATE TABLE operation_logs (
	id INTEGER NOT NULL, 
	user_id INTEGER, 
	username VARCHAR(50) NOT NULL, 
	role VARCHAR(20) NOT NULL, 
	action VARCHAR(60) NOT NULL, 
	method VARCHAR(10) NOT NULL, 
	path VARCHAR(255) NOT NULL, 
	status_code INTEGER NOT NULL, 
	ip VARCHAR(45) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE INDEX ix_operation_logs_created_at ON operation_logs (created_at);

CREATE TABLE password_resets (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	token_hash VARCHAR(64) NOT NULL, 
	expires_at DATETIME NOT NULL, 
	used_at DATETIME, 
	request_ip VARCHAR(64) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE UNIQUE INDEX ix_password_resets_token_hash ON password_resets (token_hash);

CREATE INDEX ix_password_resets_user_id ON password_resets (user_id);

CREATE TABLE role_requests (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	target_role VARCHAR(20) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	reason TEXT NOT NULL, 
	review_note TEXT NOT NULL, 
	reviewer_id INTEGER, 
	created_at DATETIME NOT NULL, 
	reviewed_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(reviewer_id) REFERENCES users (id)
);

CREATE INDEX ix_role_requests_status ON role_requests (status);

CREATE INDEX ix_role_requests_user_id ON role_requests (user_id);

CREATE TABLE saved_searches (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	name VARCHAR(50) NOT NULL, 
	conditions TEXT NOT NULL, 
	notify_on BOOLEAN NOT NULL, 
	last_notified_at DATETIME, 
	notified_house_nos TEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE INDEX ix_saved_searches_user_id ON saved_searches (user_id);

CREATE TABLE support_handoffs (
	id INTEGER NOT NULL, 
	handoff_no VARCHAR(32) NOT NULL, 
	user_id INTEGER NOT NULL, 
	question VARCHAR(500) NOT NULL, 
	reason VARCHAR(200) NOT NULL, 
	context TEXT NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	reply TEXT NOT NULL, 
	handler_id INTEGER, 
	created_at DATETIME NOT NULL, 
	replied_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(handler_id) REFERENCES users (id)
);

CREATE INDEX ix_support_handoffs_created_at ON support_handoffs (created_at);

CREATE UNIQUE INDEX ix_support_handoffs_handoff_no ON support_handoffs (handoff_no);

CREATE INDEX ix_support_handoffs_status ON support_handoffs (status);

CREATE INDEX ix_support_handoffs_user_id ON support_handoffs (user_id);

CREATE TABLE support_logs (
	id INTEGER NOT NULL, 
	user_id INTEGER, 
	session_id VARCHAR(64) NOT NULL, 
	question VARCHAR(500) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	mode VARCHAR(20) NOT NULL, 
	cited VARCHAR(255) NOT NULL, 
	top_id VARCHAR(60) NOT NULL, 
	cover FLOAT NOT NULL, 
	answer_source VARCHAR(20) NOT NULL, 
	gate_reason VARCHAR(200) NOT NULL, 
	latency_ms INTEGER NOT NULL, 
	feedback INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE INDEX ix_support_logs_created_at ON support_logs (created_at);

CREATE INDEX ix_support_logs_feedback ON support_logs (feedback);

CREATE INDEX ix_support_logs_session_id ON support_logs (session_id);

CREATE INDEX ix_support_logs_status ON support_logs (status);

CREATE INDEX ix_support_logs_top_id ON support_logs (top_id);

CREATE INDEX ix_support_logs_user_id ON support_logs (user_id);

CREATE TABLE appointments (
	id INTEGER NOT NULL, 
	house_id INTEGER NOT NULL, 
	tenant_id INTEGER, 
	contact_name VARCHAR(50) NOT NULL, 
	contact_phone VARCHAR(20) NOT NULL, 
	viewing_time DATETIME NOT NULL, 
	remark TEXT NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	reply TEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(house_id) REFERENCES houses (id), 
	FOREIGN KEY(tenant_id) REFERENCES users (id)
);

CREATE INDEX ix_appointments_house_id ON appointments (house_id);

CREATE INDEX ix_appointments_status ON appointments (status);

CREATE INDEX ix_appointments_viewing_time ON appointments (viewing_time);

CREATE TABLE browsing_history (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	house_id INTEGER NOT NULL, 
	view_count INTEGER NOT NULL, 
	viewed_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_history_user_house UNIQUE (user_id, house_id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(house_id) REFERENCES houses (id)
);

CREATE INDEX ix_browsing_history_user_id ON browsing_history (user_id);

CREATE INDEX ix_browsing_history_viewed_at ON browsing_history (viewed_at);

CREATE TABLE contracts (
	id INTEGER NOT NULL, 
	contract_no VARCHAR(32) NOT NULL, 
	house_id INTEGER NOT NULL, 
	tenant_id INTEGER NOT NULL, 
	landlord_id INTEGER NOT NULL, 
	agent_id INTEGER, 
	start_date DATETIME NOT NULL, 
	end_date DATETIME NOT NULL, 
	rent NUMERIC(10, 2) NOT NULL, 
	deposit NUMERIC(10, 2) NOT NULL, 
	pay_months INTEGER NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	content TEXT NOT NULL, 
	review_result TEXT NOT NULL, 
	signed_at DATETIME, 
	ended_at DATETIME, 
	end_reason VARCHAR(100) NOT NULL, 
	deposit_refund NUMERIC(10, 2), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (contract_no), 
	FOREIGN KEY(house_id) REFERENCES houses (id), 
	FOREIGN KEY(tenant_id) REFERENCES users (id), 
	FOREIGN KEY(landlord_id) REFERENCES users (id), 
	FOREIGN KEY(agent_id) REFERENCES users (id)
);

CREATE INDEX ix_contracts_agent_id ON contracts (agent_id);

CREATE INDEX ix_contracts_house_id ON contracts (house_id);

CREATE INDEX ix_contracts_landlord_id ON contracts (landlord_id);

CREATE INDEX ix_contracts_status ON contracts (status);

CREATE INDEX ix_contracts_tenant_id ON contracts (tenant_id);

CREATE TABLE conversations (
	id INTEGER NOT NULL, 
	house_id INTEGER NOT NULL, 
	tenant_id INTEGER NOT NULL, 
	landlord_id INTEGER NOT NULL, 
	last_message VARCHAR(100) NOT NULL, 
	last_at DATETIME NOT NULL, 
	tenant_unread INTEGER NOT NULL, 
	landlord_unread INTEGER NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_house_tenant UNIQUE (house_id, tenant_id), 
	FOREIGN KEY(house_id) REFERENCES houses (id), 
	FOREIGN KEY(tenant_id) REFERENCES users (id), 
	FOREIGN KEY(landlord_id) REFERENCES users (id)
);

CREATE INDEX ix_conversations_house_id ON conversations (house_id);

CREATE INDEX ix_conversations_landlord_id ON conversations (landlord_id);

CREATE INDEX ix_conversations_last_at ON conversations (last_at);

CREATE INDEX ix_conversations_tenant_id ON conversations (tenant_id);

CREATE TABLE favorites (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	house_id INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_user_house UNIQUE (user_id, house_id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(house_id) REFERENCES houses (id)
);

CREATE INDEX ix_favorites_user_id ON favorites (user_id);

CREATE TABLE house_delegations (
	id INTEGER NOT NULL, 
	house_id INTEGER NOT NULL, 
	landlord_id INTEGER NOT NULL, 
	agent_id INTEGER NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	initiated_by VARCHAR(10) NOT NULL, 
	commission_rate NUMERIC(5, 2) NOT NULL, 
	start_at DATETIME, 
	end_at DATETIME, 
	end_reason VARCHAR(100) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(house_id) REFERENCES houses (id), 
	FOREIGN KEY(landlord_id) REFERENCES users (id), 
	FOREIGN KEY(agent_id) REFERENCES users (id)
);

CREATE INDEX ix_house_delegations_agent_id ON house_delegations (agent_id);

CREATE INDEX ix_house_delegations_house_id ON house_delegations (house_id);

CREATE INDEX ix_house_delegations_landlord_id ON house_delegations (landlord_id);

CREATE INDEX ix_house_delegations_status ON house_delegations (status);

CREATE TABLE house_images (
	id INTEGER NOT NULL, 
	house_id INTEGER NOT NULL, 
	url VARCHAR(255) NOT NULL, 
	caption VARCHAR(100) NOT NULL, 
	sort_order INTEGER NOT NULL, 
	is_cover BOOLEAN NOT NULL, 
	width INTEGER NOT NULL, 
	height INTEGER NOT NULL, 
	file_size INTEGER NOT NULL, 
	hash VARCHAR(64) NOT NULL, 
	audit_status VARCHAR(20) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(house_id) REFERENCES houses (id)
);

CREATE INDEX ix_house_images_hash ON house_images (hash);

CREATE INDEX ix_house_images_house_id ON house_images (house_id);

CREATE TABLE house_reports (
	id INTEGER NOT NULL, 
	report_no VARCHAR(32) NOT NULL, 
	reporter_id INTEGER NOT NULL, 
	house_id INTEGER NOT NULL, 
	kind VARCHAR(20) NOT NULL, 
	description TEXT NOT NULL, 
	evidence TEXT NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	handler_id INTEGER, 
	conclusion VARCHAR(300) NOT NULL, 
	handled_at DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (report_no), 
	FOREIGN KEY(reporter_id) REFERENCES users (id), 
	FOREIGN KEY(house_id) REFERENCES houses (id), 
	FOREIGN KEY(handler_id) REFERENCES users (id)
);

CREATE INDEX ix_house_reports_house_id ON house_reports (house_id);

CREATE INDEX ix_house_reports_kind ON house_reports (kind);

CREATE INDEX ix_house_reports_reporter_id ON house_reports (reporter_id);

CREATE INDEX ix_house_reports_status ON house_reports (status);

CREATE TABLE repairs (
	id INTEGER NOT NULL, 
	house_id INTEGER NOT NULL, 
	tenant_id INTEGER, 
	contact_name VARCHAR(50) NOT NULL, 
	contact_phone VARCHAR(20) NOT NULL, 
	content TEXT NOT NULL, 
	category VARCHAR(30) NOT NULL, 
	urgency VARCHAR(20) NOT NULL, 
	assignee VARCHAR(50) NOT NULL, 
	analysis TEXT NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(house_id) REFERENCES houses (id), 
	FOREIGN KEY(tenant_id) REFERENCES users (id)
);

CREATE INDEX ix_repairs_category ON repairs (category);

CREATE INDEX ix_repairs_house_id ON repairs (house_id);

CREATE INDEX ix_repairs_status ON repairs (status);

CREATE INDEX ix_repairs_urgency ON repairs (urgency);

CREATE TABLE agent_commissions (
	id INTEGER NOT NULL, 
	contract_id INTEGER NOT NULL, 
	delegation_id INTEGER, 
	agent_id INTEGER NOT NULL, 
	base_amount NUMERIC(10, 2) NOT NULL, 
	rate NUMERIC(5, 2) NOT NULL, 
	amount NUMERIC(10, 2) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	settled_at DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(contract_id) REFERENCES contracts (id), 
	FOREIGN KEY(delegation_id) REFERENCES house_delegations (id), 
	FOREIGN KEY(agent_id) REFERENCES users (id)
);

CREATE INDEX ix_agent_commissions_agent_id ON agent_commissions (agent_id);

CREATE INDEX ix_agent_commissions_contract_id ON agent_commissions (contract_id);

CREATE INDEX ix_agent_commissions_status ON agent_commissions (status);

CREATE TABLE bills (
	id INTEGER NOT NULL, 
	bill_no VARCHAR(32) NOT NULL, 
	contract_id INTEGER NOT NULL, 
	type VARCHAR(20) NOT NULL, 
	period VARCHAR(20) NOT NULL, 
	amount NUMERIC(10, 2) NOT NULL, 
	due_date DATETIME NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	paid_at DATETIME, 
	remark VARCHAR(200) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (bill_no), 
	FOREIGN KEY(contract_id) REFERENCES contracts (id)
);

CREATE INDEX ix_bills_contract_id ON bills (contract_id);

CREATE INDEX ix_bills_due_date ON bills (due_date);

CREATE INDEX ix_bills_status ON bills (status);

CREATE INDEX ix_bills_type ON bills (type);

CREATE TABLE chat_lines (
	id INTEGER NOT NULL, 
	conversation_id INTEGER NOT NULL, 
	sender_id INTEGER NOT NULL, 
	content TEXT NOT NULL, 
	kind VARCHAR(10) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(conversation_id) REFERENCES conversations (id), 
	FOREIGN KEY(sender_id) REFERENCES users (id)
);

CREATE INDEX ix_chat_lines_conversation_id ON chat_lines (conversation_id);

CREATE TABLE repair_images (
	id INTEGER NOT NULL, 
	repair_id INTEGER NOT NULL, 
	url VARCHAR(255) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(repair_id) REFERENCES repairs (id)
);

CREATE INDEX ix_repair_images_repair_id ON repair_images (repair_id);

CREATE TABLE repair_records (
	id INTEGER NOT NULL, 
	repair_id INTEGER NOT NULL, 
	worker_id INTEGER, 
	hours NUMERIC(10, 2) NOT NULL, 
	labor_cost NUMERIC(10, 2) NOT NULL, 
	item_cost NUMERIC(10, 2) NOT NULL, 
	note TEXT NOT NULL, 
	finished_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (repair_id), 
	FOREIGN KEY(repair_id) REFERENCES repairs (id), 
	FOREIGN KEY(worker_id) REFERENCES repair_workers (id)
);

CREATE TABLE reviews (
	id INTEGER NOT NULL, 
	contract_id INTEGER NOT NULL, 
	house_id INTEGER NOT NULL, 
	tenant_id INTEGER NOT NULL, 
	rating INTEGER NOT NULL, 
	rating_house INTEGER NOT NULL, 
	rating_landlord INTEGER NOT NULL, 
	rating_agent INTEGER NOT NULL, 
	agent_id INTEGER, 
	content_landlord TEXT NOT NULL, 
	content_agent TEXT NOT NULL, 
	content TEXT NOT NULL, 
	images TEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	deleted_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_review_contract UNIQUE (contract_id), 
	FOREIGN KEY(contract_id) REFERENCES contracts (id), 
	FOREIGN KEY(house_id) REFERENCES houses (id), 
	FOREIGN KEY(tenant_id) REFERENCES users (id), 
	FOREIGN KEY(agent_id) REFERENCES users (id)
);

CREATE INDEX ix_reviews_agent_id ON reviews (agent_id);

CREATE INDEX ix_reviews_house_id ON reviews (house_id);

CREATE TABLE viewing_records (
	id INTEGER NOT NULL, 
	appointment_id INTEGER, 
	house_id INTEGER NOT NULL, 
	tenant_id INTEGER NOT NULL, 
	agent_id INTEGER NOT NULL, 
	showed_up BOOLEAN NOT NULL, 
	duration_minutes INTEGER NOT NULL, 
	feedback TEXT NOT NULL, 
	concerns VARCHAR(200) NOT NULL, 
	intention VARCHAR(10) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(appointment_id) REFERENCES appointments (id), 
	FOREIGN KEY(house_id) REFERENCES houses (id), 
	FOREIGN KEY(tenant_id) REFERENCES users (id), 
	FOREIGN KEY(agent_id) REFERENCES users (id)
);

CREATE INDEX ix_viewing_records_agent_id ON viewing_records (agent_id);

CREATE INDEX ix_viewing_records_appointment_id ON viewing_records (appointment_id);

CREATE INDEX ix_viewing_records_created_at ON viewing_records (created_at);

CREATE INDEX ix_viewing_records_house_id ON viewing_records (house_id);

CREATE INDEX ix_viewing_records_intention ON viewing_records (intention);

CREATE INDEX ix_viewing_records_tenant_id ON viewing_records (tenant_id);

CREATE TABLE payments (
	id INTEGER NOT NULL, 
	trade_no VARCHAR(40) NOT NULL, 
	bill_id INTEGER NOT NULL, 
	user_id INTEGER, 
	amount NUMERIC(10, 2) NOT NULL, 
	method VARCHAR(20) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	idempotency_key VARCHAR(64), 
	failure_reason VARCHAR(200) NOT NULL, 
	gateway VARCHAR(20) NOT NULL, 
	gateway_trade_no VARCHAR(64), 
	notify_payload TEXT NOT NULL, 
	notified_at DATETIME, 
	expire_at DATETIME, 
	created_at DATETIME NOT NULL, 
	paid_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (trade_no), 
	FOREIGN KEY(bill_id) REFERENCES bills (id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	UNIQUE (idempotency_key)
);

CREATE UNIQUE INDEX ix_payments_bill_id ON payments (bill_id);

CREATE INDEX ix_payments_gateway_trade_no ON payments (gateway_trade_no);

CREATE INDEX ix_payments_status ON payments (status);

CREATE TABLE repair_item_usages (
	id INTEGER NOT NULL, 
	record_id INTEGER NOT NULL, 
	item_id INTEGER NOT NULL, 
	item_name VARCHAR(50) NOT NULL, 
	quantity INTEGER NOT NULL, 
	unit_price NUMERIC(10, 2) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(record_id) REFERENCES repair_records (id), 
	FOREIGN KEY(item_id) REFERENCES repair_items (id)
);

CREATE INDEX ix_repair_item_usages_item_id ON repair_item_usages (item_id);

CREATE INDEX ix_repair_item_usages_record_id ON repair_item_usages (record_id);

CREATE TABLE refunds (
	id INTEGER NOT NULL, 
	refund_no VARCHAR(40) NOT NULL, 
	contract_id INTEGER NOT NULL, 
	payment_id INTEGER, 
	original_amount NUMERIC(10, 2) NOT NULL, 
	deduct_amount NUMERIC(10, 2) NOT NULL, 
	refund_amount NUMERIC(10, 2) NOT NULL, 
	reason VARCHAR(200) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	refunded_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (refund_no), 
	FOREIGN KEY(contract_id) REFERENCES contracts (id), 
	FOREIGN KEY(payment_id) REFERENCES payments (id)
);

CREATE INDEX ix_refunds_contract_id ON refunds (contract_id);

CREATE INDEX ix_refunds_status ON refunds (status);
