-- 基于 AI 智能体的房屋租赁系统（大连智租） 建库脚本 · MySQL 8.0+
-- 大连理工大学 实习实训 第 6 小组
--
-- **这个文件是生成的，不要手改。** 改了也会被下一次生成覆盖，
-- 而且会和真正在跑的结构分叉。
--   源：server/app/models.py（SQLAlchemy 模型）
--   生成：python scripts/gen_db_sql.py
--   校验：python scripts/gen_db_sql.py --check（测试套件里也跑这一条）
--
-- 共 36 张表、395 个字段。建表顺序是依赖拓扑序，被引用的表先建。
--
-- 列默认值有意不写进 DDL：模型里的 default 是 Python 侧的，由 ORM 插入时填。
-- 写进来就等于多一份由数据库执行的语义，两边不一致时谁说了算会说不清。

CREATE DATABASE IF NOT EXISTS `rental`
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE `rental`;

-- 重复执行时先删干净。关掉外键检查是因为删除顺序与建表顺序相反，
-- 逐张删会被外键挡住。
SET FOREIGN_KEY_CHECKS = 0;
DROP TABLE IF EXISTS `refunds`;
DROP TABLE IF EXISTS `repair_item_usages`;
DROP TABLE IF EXISTS `payments`;
DROP TABLE IF EXISTS `viewing_records`;
DROP TABLE IF EXISTS `reviews`;
DROP TABLE IF EXISTS `repair_records`;
DROP TABLE IF EXISTS `repair_images`;
DROP TABLE IF EXISTS `chat_lines`;
DROP TABLE IF EXISTS `bills`;
DROP TABLE IF EXISTS `agent_commissions`;
DROP TABLE IF EXISTS `repairs`;
DROP TABLE IF EXISTS `house_reports`;
DROP TABLE IF EXISTS `house_images`;
DROP TABLE IF EXISTS `house_delegations`;
DROP TABLE IF EXISTS `favorites`;
DROP TABLE IF EXISTS `conversations`;
DROP TABLE IF EXISTS `contracts`;
DROP TABLE IF EXISTS `browsing_history`;
DROP TABLE IF EXISTS `appointments`;
DROP TABLE IF EXISTS `support_logs`;
DROP TABLE IF EXISTS `support_handoffs`;
DROP TABLE IF EXISTS `saved_searches`;
DROP TABLE IF EXISTS `role_requests`;
DROP TABLE IF EXISTS `password_resets`;
DROP TABLE IF EXISTS `operation_logs`;
DROP TABLE IF EXISTS `notices`;
DROP TABLE IF EXISTS `messages`;
DROP TABLE IF EXISTS `houses`;
DROP TABLE IF EXISTS `content_flags`;
DROP TABLE IF EXISTS `chat_messages`;
DROP TABLE IF EXISTS `agent_profiles`;
DROP TABLE IF EXISTS `users`;
DROP TABLE IF EXISTS `system_configs`;
DROP TABLE IF EXISTS `repair_workers`;
DROP TABLE IF EXISTS `repair_items`;
DROP TABLE IF EXISTS `faqs`;
SET FOREIGN_KEY_CHECKS = 1;

CREATE TABLE faqs (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	code VARCHAR(60) NOT NULL COMMENT '检索侧标识，如 faq-deposit-refund。回答的引用用它，可读性直接影响排查效率', 
	question VARCHAR(200) NOT NULL COMMENT '也是检索的一路独立索引：客服场景「问句 vs 问句」信号最强', 
	answer TEXT NOT NULL COMMENT '首句必须是一句话结论，大模型不可用时抽取式回答只取首句', 
	category VARCHAR(30) NOT NULL COMMENT '押金／费用／看房／报修／退租／合同／缴费／账号／房东', 
	aliases VARCHAR(255) NOT NULL COMMENT '同义问法，/ 分隔。每条别名单独建索引并取最大值，不能拼成一篇长文档——BM25 会按长度惩罚，别名写得越多排得越靠后', 
	is_published BOOL NOT NULL COMMENT '下架后智能体下一次刷新即不再检索', 
	sort_order INTEGER NOT NULL, 
	hit_count INTEGER NOT NULL COMMENT '被智能体引用的次数，管理端据此看哪些条目写了没人用', 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
) COMMENT='客服知识条目 — AI-03 智能客服的权威语料源。与 notices 的分工：公告是面向全体用户的通告（有时效、会过期），FAQ 是平台规则的常驻问答，两者生命周期不同，混一张表会让「公告过期下架」和「FAQ 长期有效」互相打架。语料的编写源是 agent/knowledge/faq.md，由 seed.py 导入本表；导入之后数据库才是运行时的权威源' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_faqs_category ON faqs (category);

CREATE UNIQUE INDEX ix_faqs_code ON faqs (code);

CREATE INDEX ix_faqs_is_published ON faqs (is_published);

CREATE TABLE repair_items (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	name VARCHAR(50) NOT NULL, 
	unit VARCHAR(10) NOT NULL, 
	unit_price NUMERIC(10, 2) NOT NULL, 
	stock INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
) COMMENT='维修物品 — F-09 的耗材档案，含库存' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE repair_workers (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	name VARCHAR(50) NOT NULL, 
	phone VARCHAR(20) NOT NULL, 
	skill VARCHAR(30) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
) COMMENT='维修人员 — F-09 维修数据的人员档案' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE system_configs (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	`key` VARCHAR(60) NOT NULL, 
	value VARCHAR(255) NOT NULL COMMENT '统一字符串，类型由 value_type 描述', 
	value_type VARCHAR(10) NOT NULL, 
	label VARCHAR(60) NOT NULL, 
	description VARCHAR(200) NOT NULL, 
	group_name VARCHAR(30) NOT NULL, 
	is_public BOOL NOT NULL COMMENT '为真才会出现在公开配置接口里', 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (`key`)
) COMMENT='系统配置 — M-09，值统一用字符串存 + 类型标记' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE users (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	username VARCHAR(50) NOT NULL, 
	password_hash VARCHAR(255) NOT NULL COMMENT 'BCrypt 哈希；环境无 bcrypt 时退回 PBKDF2-HMAC-SHA256', 
	real_name VARCHAR(50) NOT NULL, 
	phone VARCHAR(20) NOT NULL, 
	email VARCHAR(100) NOT NULL, 
	token_valid_after DATETIME, 
	id_card_hash VARCHAR(64) NOT NULL, 
	id_card_masked VARCHAR(32) NOT NULL, 
	`role` VARCHAR(20) NOT NULL COMMENT 'tenant 租客 / landlord 房东 / agent 经纪人 / admin 管理员。agent 本身不给任何权限，接单须资质过审（J-01）', 
	status VARCHAR(20) NOT NULL COMMENT 'active 正常 / disabled 已禁用', 
	accept_contact BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (username)
) COMMENT='用户 — 三种角色共用一张表，靠 role 区分租客、房东、管理员' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_users_id_card_hash ON users (id_card_hash);

CREATE TABLE agent_profiles (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	user_id INTEGER NOT NULL, 
	license_status VARCHAR(20) NOT NULL COMMENT 'unsubmitted / pending / approved / rejected —— 沿用 K-07 的核验四态，不另发明一套', 
	license_no VARCHAR(40) NOT NULL, 
	license_images TEXT NOT NULL, 
	reviewed_by INTEGER COMMENT '审核人。审计需要，不能只留结论', 
	reviewed_at DATETIME, 
	reject_reason VARCHAR(200) NOT NULL COMMENT '驳回必填。只回「未通过」三个字，被驳回的人不知道该补什么，只会反复提交同一份材料', 
	service_districts VARCHAR(120) NOT NULL COMMENT '主营区域，逗号分隔；空表示不限', 
	intro VARCHAR(300) NOT NULL, 
	years INTEGER NOT NULL, 
	accepting BOOL NOT NULL COMMENT '是否接受新委托。关掉只挡新邀请，已在托房源不受影响——休假不等于甩手', 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(reviewed_by) REFERENCES users (id)
) COMMENT='中介档案 — J-01。与 users 一对一。表里没有任何统计列——带看数、成交数、好评率全部由明细实时聚合（原则 1.4 / 需求 R-05）；存下来就意味着有人能改，也意味着迟早会和明细对不上' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_agent_profiles_license_status ON agent_profiles (license_status);

CREATE UNIQUE INDEX ix_agent_profiles_user_id ON agent_profiles (user_id);

CREATE TABLE chat_messages (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	session_id VARCHAR(64) NOT NULL COMMENT '会话编号，查询时必须与 user_id 一起过滤', 
	user_id INTEGER, 
	`role` VARCHAR(20) NOT NULL, 
	content TEXT NOT NULL, 
	intent VARCHAR(30) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
) COMMENT='对话记录 — AI-09 多轮记忆的持久化，只存原文不存槽位' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_chat_messages_session_id ON chat_messages (session_id);

CREATE TABLE content_flags (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	scene VARCHAR(20) NOT NULL, 
	target_id INTEGER NOT NULL, 
	target_key VARCHAR(64) NOT NULL, 
	author_id INTEGER, 
	author_name VARCHAR(50) NOT NULL, 
	excerpt VARCHAR(300) NOT NULL COMMENT '内容摘要，冗余存储，原内容可能已被作者改掉', 
	categories VARCHAR(120) NOT NULL, 
	reasons TEXT NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	handled_by VARCHAR(50) NOT NULL, 
	created_at DATETIME NOT NULL, 
	handled_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(author_id) REFERENCES users (id)
) COMMENT='内容复核队列 — M-08，只记机器拿不准的，确定违规的根本不入库' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_content_flags_created_at ON content_flags (created_at);

CREATE INDEX ix_content_flags_scene ON content_flags (scene);

CREATE INDEX ix_content_flags_status ON content_flags (status);

CREATE TABLE houses (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	house_no VARCHAR(32) NOT NULL, 
	title VARCHAR(120) NOT NULL, 
	landlord_id INTEGER NOT NULL, 
	province VARCHAR(30) NOT NULL, 
	city VARCHAR(30) NOT NULL, 
	district VARCHAR(30) NOT NULL, 
	community VARCHAR(60) NOT NULL, 
	address VARCHAR(200) NOT NULL, 
	rent_price NUMERIC(10, 2) NOT NULL COMMENT '统一 decimal(10,2)，不用 int 或 varchar', 
	deposit NUMERIC(10, 2) NOT NULL, 
	area NUMERIC(10, 2) NOT NULL, 
	layout VARCHAR(20) NOT NULL, 
	rent_type VARCHAR(20) NOT NULL, 
	floor VARCHAR(20) NOT NULL, 
	orientation VARCHAR(20) NOT NULL, 
	description TEXT NOT NULL, 
	tags VARCHAR(200) NOT NULL COMMENT '逗号分隔的标签串，读取时拆成数组', 
	cover_image VARCHAR(255) NOT NULL, 
	elevator BOOL NOT NULL, 
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
	status VARCHAR(20) NOT NULL COMMENT 'pending 待审核 / available 可租 / rented 已租 / offline 已下架', 
	view_count INTEGER NOT NULL, 
	published_at DATETIME NOT NULL, 
	approved_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (house_no), 
	FOREIGN KEY(landlord_id) REFERENCES users (id)
) COMMENT='房源 — 系统的核心实体，状态机为 pending→available→rented→offline' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

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
	id INTEGER NOT NULL AUTO_INCREMENT, 
	user_id INTEGER NOT NULL, 
	type VARCHAR(20) NOT NULL, 
	title VARCHAR(120) NOT NULL, 
	content TEXT NOT NULL, 
	link_no VARCHAR(32) NOT NULL, 
	is_read BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
) COMMENT='站内消息 — 预约结果、账单到期、工单进度的通知' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_messages_is_read ON messages (is_read);

CREATE INDEX ix_messages_type ON messages (type);

CREATE INDEX ix_messages_user_id ON messages (user_id);

CREATE TABLE notices (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	title VARCHAR(120) NOT NULL, 
	summary VARCHAR(255) NOT NULL, 
	content TEXT NOT NULL, 
	category VARCHAR(30) NOT NULL, 
	publisher_id INTEGER, 
	is_published BOOL NOT NULL, 
	view_count INTEGER NOT NULL, 
	published_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(publisher_id) REFERENCES users (id)
) COMMENT='平台公告 — 同时是 AI-03 智能客服的知识语料' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_notices_category ON notices (category);

CREATE INDEX ix_notices_is_published ON notices (is_published);

CREATE TABLE operation_logs (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	user_id INTEGER, 
	username VARCHAR(50) NOT NULL COMMENT '冗余存储，用户改名或被删后仍查得到当时是谁', 
	`role` VARCHAR(20) NOT NULL, 
	action VARCHAR(60) NOT NULL, 
	method VARCHAR(10) NOT NULL, 
	path VARCHAR(255) NOT NULL, 
	status_code INTEGER NOT NULL, 
	ip VARCHAR(45) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
) COMMENT='操作日志 — M-07，只记改变状态的操作，不记请求体' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_operation_logs_created_at ON operation_logs (created_at);

CREATE TABLE password_resets (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	user_id INTEGER NOT NULL, 
	token_hash VARCHAR(64) NOT NULL COMMENT '只存哈希。存明文等于把一份可直接改密码的钥匙留在库里', 
	expires_at DATETIME NOT NULL, 
	used_at DATETIME, 
	request_ip VARCHAR(64) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
) COMMENT='找回密码令牌 — K-11。只存哈希不存明文令牌，库被读走也无法直接冒用；重置成功即吊销该用户全部旧令牌与其余未用链接' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE UNIQUE INDEX ix_password_resets_token_hash ON password_resets (token_hash);

CREATE INDEX ix_password_resets_user_id ON password_resets (user_id);

CREATE TABLE role_requests (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	user_id INTEGER NOT NULL, 
	target_role VARCHAR(20) NOT NULL COMMENT '申请成为 landlord 房东 / agent 经纪人', 
	status VARCHAR(20) NOT NULL COMMENT 'pending 待审 / approved 通过 / rejected 驳回 / withdrawn 用户自己撤回。获批之前仍是租客，找房、收藏、预约照常，只是发不了房源', 
	reason TEXT NOT NULL, 
	review_note TEXT NOT NULL COMMENT '驳回必填——只说「不通过」等于让用户去猜该补什么', 
	reviewer_id INTEGER, 
	created_at DATETIME NOT NULL, 
	reviewed_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(reviewer_id) REFERENCES users (id)
) COMMENT='角色升级申请 — 《改进清单 2026-09-10》D1。「成为房东」原先是点一下当场生效，而房东是合同甲方，平台至少要看一眼这个人是谁，所以改成申请制。退回不需要审核（同一份清单 D2）：放弃权限不是获得权限，唯一前置是手上没有在租房源与生效合同，那是数据完整性要求，不是审批' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_role_requests_status ON role_requests (status);

CREATE INDEX ix_role_requests_user_id ON role_requests (user_id);

CREATE TABLE saved_searches (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	user_id INTEGER NOT NULL, 
	name VARCHAR(50) NOT NULL, 
	conditions TEXT NOT NULL COMMENT '筛选条件的 JSON 快照。匹配时交给 house_query 执行，不在这里另写一份判断', 
	notify_on BOOL NOT NULL, 
	last_notified_at DATETIME, 
	notified_house_nos TEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
) COMMENT='保存的搜索 — K-09。匹配逻辑必须与列表页共用同一套条件判断（house_query.apply_filters），另写一份判断迟早会两边不一致' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_saved_searches_user_id ON saved_searches (user_id);

CREATE TABLE support_handoffs (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	handoff_no VARCHAR(32) NOT NULL COMMENT 'HD + 时间戳，与合同、账单编号同一套生成规则', 
	user_id INTEGER NOT NULL COMMENT '必填：管理员回复时得知道回给谁，所以转人工要求登录', 
	question VARCHAR(500) NOT NULL, 
	reason VARCHAR(200) NOT NULL COMMENT '智能体为什么没答上来，供管理员判断是语料缺失还是问题超范围', 
	context TEXT NOT NULL COMMENT '最近几轮对话', 
	status VARCHAR(20) NOT NULL COMMENT 'pending／replied／closed', 
	reply TEXT NOT NULL, 
	handler_id INTEGER, 
	created_at DATETIME NOT NULL, 
	replied_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(handler_id) REFERENCES users (id)
) COMMENT='转人工工单 — 客服闭环里最不该省的一环：没有它，拒答就是死路一条，用户被告知「我不知道」之后无处可去。建单后给每位管理员发一条站内消息；管理员回复后，回复同样通过 messages 送达用户——复用现成的消息通道，不另造一套通知机制' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_support_handoffs_created_at ON support_handoffs (created_at);

CREATE UNIQUE INDEX ix_support_handoffs_handoff_no ON support_handoffs (handoff_no);

CREATE INDEX ix_support_handoffs_status ON support_handoffs (status);

CREATE INDEX ix_support_handoffs_user_id ON support_handoffs (user_id);

CREATE TABLE support_logs (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	user_id INTEGER COMMENT '允许为空：未登录用户的提问同样是「哪些问题答不上来」的证据', 
	session_id VARCHAR(64) NOT NULL, 
	question VARCHAR(500) NOT NULL, 
	status VARCHAR(20) NOT NULL COMMENT 'answered／refused／escalate／account／need_login', 
	mode VARCHAR(20) NOT NULL COMMENT 'lexical／vector+lexical／account', 
	cited VARCHAR(255) NOT NULL COMMENT '引用的片 id，逗号分隔；拒答时为空', 
	top_id VARCHAR(60) NOT NULL COMMENT '检索首位，用于分析「排对了但被闸门拦下」的情况', 
	cover FLOAT NOT NULL COMMENT '可答性闸门的覆盖率判据', 
	answer_source VARCHAR(20) NOT NULL COMMENT 'llm／extractive／rules', 
	gate_reason VARCHAR(200) NOT NULL COMMENT '拒答原因，直接显示在管理端', 
	latency_ms INTEGER NOT NULL, 
	feedback INTEGER NOT NULL COMMENT '1 有用／−1 没用／0 未评价。−1 比 1 值钱，它指出哪条写得不对', 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
) COMMENT='客服问答日志 — 存它不是为了排错，是为了回答「这个客服到底行不行」：拒答率多少、哪些问题反复问却答不上来、命中的都是哪几条 FAQ。没有这张表，语料该往哪儿补全靠拍脑袋。高频机器写入，已在 oplog.SKIP_PATHS 里排除，不进操作日志' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_support_logs_created_at ON support_logs (created_at);

CREATE INDEX ix_support_logs_feedback ON support_logs (feedback);

CREATE INDEX ix_support_logs_session_id ON support_logs (session_id);

CREATE INDEX ix_support_logs_status ON support_logs (status);

CREATE INDEX ix_support_logs_top_id ON support_logs (top_id);

CREATE INDEX ix_support_logs_user_id ON support_logs (user_id);

CREATE TABLE appointments (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	house_id INTEGER NOT NULL, 
	tenant_id INTEGER, 
	contact_name VARCHAR(50) NOT NULL, 
	contact_phone VARCHAR(20) NOT NULL, 
	viewing_time DATETIME NOT NULL COMMENT '必须 datetime，date 表达不了「下午两点」', 
	remark TEXT NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	reply TEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(house_id) REFERENCES houses (id), 
	FOREIGN KEY(tenant_id) REFERENCES users (id)
) COMMENT='看房预约 — 时段冲突由接口层按配置的时间窗口判断' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_appointments_house_id ON appointments (house_id);

CREATE INDEX ix_appointments_status ON appointments (status);

CREATE INDEX ix_appointments_viewing_time ON appointments (viewing_time);

CREATE TABLE browsing_history (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	user_id INTEGER NOT NULL, 
	house_id INTEGER NOT NULL, 
	view_count INTEGER NOT NULL, 
	viewed_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_history_user_house UNIQUE (user_id, house_id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(house_id) REFERENCES houses (id)
) COMMENT='浏览足迹 — AI-07 房源推荐的输入' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_browsing_history_user_id ON browsing_history (user_id);

CREATE INDEX ix_browsing_history_viewed_at ON browsing_history (viewed_at);

CREATE TABLE contracts (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	contract_no VARCHAR(32) NOT NULL, 
	house_id INTEGER NOT NULL, 
	tenant_id INTEGER NOT NULL, 
	landlord_id INTEGER NOT NULL, 
	agent_id INTEGER COMMENT '促成这单的中介（J-06）。甲方仍是房东，中介不是合同一方，此列只用于算佣金与「累计成交」，不进合同正文；房东自管的合同为空', 
	start_date DATETIME NOT NULL, 
	end_date DATETIME NOT NULL, 
	rent NUMERIC(10, 2) NOT NULL, 
	deposit NUMERIC(10, 2) NOT NULL, 
	pay_months INTEGER NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	content TEXT NOT NULL, 
	review_result TEXT NOT NULL COMMENT 'AI-02 合同审阅的结论快照', 
	request_type VARCHAR(10) NOT NULL, 
	request_by VARCHAR(10) NOT NULL, 
	request_months INTEGER NOT NULL, 
	request_reason VARCHAR(100) NOT NULL, 
	requested_at DATETIME, 
	request_deduct NUMERIC(10, 2) NOT NULL, 
	signed_at DATETIME, 
	ended_at DATETIME, 
	end_reason VARCHAR(100) NOT NULL, 
	deposit_refund NUMERIC(10, 2) COMMENT '退租时实退的押金，扣款不得超过押金且不得为负', 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (contract_no), 
	FOREIGN KEY(house_id) REFERENCES houses (id), 
	FOREIGN KEY(tenant_id) REFERENCES users (id), 
	FOREIGN KEY(landlord_id) REFERENCES users (id), 
	FOREIGN KEY(agent_id) REFERENCES users (id)
) COMMENT='租赁合同 — 状态机为 pending→active→ended，另有 cancelled 分支' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_contracts_agent_id ON contracts (agent_id);

CREATE INDEX ix_contracts_house_id ON contracts (house_id);

CREATE INDEX ix_contracts_landlord_id ON contracts (landlord_id);

CREATE INDEX ix_contracts_status ON contracts (status);

CREATE INDEX ix_contracts_tenant_id ON contracts (tenant_id);

CREATE TABLE conversations (
	id INTEGER NOT NULL AUTO_INCREMENT, 
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
) COMMENT='咨询会话 — 租客与房源负责人的一对一会话。委托期间「对方」是受托中介（J-03），但会话本身仍是两方，不因换人而断' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_conversations_house_id ON conversations (house_id);

CREATE INDEX ix_conversations_landlord_id ON conversations (landlord_id);

CREATE INDEX ix_conversations_last_at ON conversations (last_at);

CREATE INDEX ix_conversations_tenant_id ON conversations (tenant_id);

CREATE TABLE favorites (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	user_id INTEGER NOT NULL, 
	house_id INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_user_house UNIQUE (user_id, house_id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(house_id) REFERENCES houses (id)
) COMMENT='收藏 — 租客与房源的多对多关系' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_favorites_user_id ON favorites (user_id);

CREATE TABLE house_delegations (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	house_id INTEGER NOT NULL, 
	landlord_id INTEGER NOT NULL COMMENT '冗余存储（原则 1.6）：房源日后转手也不影响这条委托当时是谁签的', 
	agent_id INTEGER NOT NULL, 
	status VARCHAR(20) NOT NULL COMMENT 'pending / active / rejected / ended', 
	initiated_by VARCHAR(10) NOT NULL COMMENT 'landlord / agent。决定该由哪一方来确认', 
	commission_rate NUMERIC(5, 2) NOT NULL COMMENT '佣金比例，按月租金的百分比', 
	start_at DATETIME, 
	end_at DATETIME, 
	end_reason VARCHAR(100) NOT NULL COMMENT '解除必填，任一方解除都要说明', 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(house_id) REFERENCES houses (id), 
	FOREIGN KEY(landlord_id) REFERENCES users (id), 
	FOREIGN KEY(agent_id) REFERENCES users (id)
) COMMENT='房源委托 — J-04，中介这条链的核心表，也是权限判断的唯一依据——can_manage_house() 每次鉴权都要问它。状态机 pending→active→ended，另有 rejected 分支；双向确认，单方发起只到 pending，须另一方点头才 active' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_house_delegations_agent_id ON house_delegations (agent_id);

CREATE INDEX ix_house_delegations_house_id ON house_delegations (house_id);

CREATE INDEX ix_house_delegations_landlord_id ON house_delegations (landlord_id);

CREATE INDEX ix_house_delegations_status ON house_delegations (status);

CREATE TABLE house_images (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	house_id INTEGER NOT NULL, 
	url VARCHAR(255) NOT NULL, 
	caption VARCHAR(100) NOT NULL, 
	sort_order INTEGER NOT NULL, 
	is_cover BOOL NOT NULL, 
	width INTEGER NOT NULL, 
	height INTEGER NOT NULL, 
	file_size INTEGER NOT NULL, 
	hash VARCHAR(64) NOT NULL, 
	audit_status VARCHAR(20) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(house_id) REFERENCES houses (id)
) COMMENT='房源相册 — is_cover 标记封面，列表页只取封面，详情页取全部' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_house_images_hash ON house_images (hash);

CREATE INDEX ix_house_images_house_id ON house_images (house_id);

CREATE TABLE house_reports (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	report_no VARCHAR(32) NOT NULL, 
	reporter_id INTEGER NOT NULL, 
	house_id INTEGER NOT NULL, 
	kind VARCHAR(20) NOT NULL, 
	description TEXT NOT NULL, 
	evidence TEXT NOT NULL, 
	status VARCHAR(20) NOT NULL COMMENT 'pending 待处理 / accepted 属实 / rejected 不属实。判定与处置分开——判「属实」不自动下架，下架是另一个动作', 
	handler_id INTEGER, 
	conclusion VARCHAR(300) NOT NULL, 
	handled_at DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (report_no), 
	FOREIGN KEY(reporter_id) REFERENCES users (id), 
	FOREIGN KEY(house_id) REFERENCES houses (id), 
	FOREIGN KEY(handler_id) REFERENCES users (id)
) COMMENT='房源举报 — K-08。刻意不复用 content_flags：那张表是机器复核队列，这张是用户举报，发起人、处理流程、回执要求都不同。判定与处置分开——判「属实」不自动下架' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_house_reports_house_id ON house_reports (house_id);

CREATE INDEX ix_house_reports_kind ON house_reports (kind);

CREATE INDEX ix_house_reports_reporter_id ON house_reports (reporter_id);

CREATE INDEX ix_house_reports_status ON house_reports (status);

CREATE TABLE repairs (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	house_id INTEGER NOT NULL, 
	tenant_id INTEGER, 
	contact_name VARCHAR(50) NOT NULL, 
	contact_phone VARCHAR(20) NOT NULL, 
	content TEXT NOT NULL, 
	category VARCHAR(30) NOT NULL, 
	urgency VARCHAR(20) NOT NULL COMMENT '紧急 / 一般 / 可延后，由 AI-04 判定', 
	assignee VARCHAR(50) NOT NULL, 
	analysis TEXT NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(house_id) REFERENCES houses (id), 
	FOREIGN KEY(tenant_id) REFERENCES users (id)
) COMMENT='报修工单 — category/urgency/assignee 三项由 AI-04 分析填充' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_repairs_category ON repairs (category);

CREATE INDEX ix_repairs_house_id ON repairs (house_id);

CREATE INDEX ix_repairs_status ON repairs (status);

CREATE INDEX ix_repairs_urgency ON repairs (urgency);

CREATE TABLE agent_commissions (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	contract_id INTEGER NOT NULL, 
	delegation_id INTEGER COMMENT '允许为空：委托记录若被清理，佣金本身仍应留存', 
	agent_id INTEGER NOT NULL, 
	base_amount NUMERIC(10, 2) NOT NULL COMMENT '计佣基数（月租金）', 
	rate NUMERIC(5, 2) NOT NULL, 
	amount NUMERIC(10, 2) NOT NULL COMMENT '基数与比例分开存：只存最终金额，事后想核对「这笔是怎么算出来的」就查不了了', 
	status VARCHAR(20) NOT NULL COMMENT 'pending 待结算 / settled 已结算 / cancelled 已取消', 
	settled_at DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(contract_id) REFERENCES contracts (id), 
	FOREIGN KEY(delegation_id) REFERENCES house_delegations (id), 
	FOREIGN KEY(agent_id) REFERENCES users (id)
) COMMENT='佣金 — J-06。刻意不复用 bills / payments：那两张表描述的是「租客付给房东」，佣金是「房东付给中介」，付款方、收款方、触发条件都不同，塞进去只会让两边字段互相为空。结算不接真实资金，与 D-1 同一条边界：状态机做全，钱线下结' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_agent_commissions_agent_id ON agent_commissions (agent_id);

CREATE INDEX ix_agent_commissions_contract_id ON agent_commissions (contract_id);

CREATE INDEX ix_agent_commissions_status ON agent_commissions (status);

CREATE TABLE bills (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	bill_no VARCHAR(32) NOT NULL, 
	contract_id INTEGER NOT NULL, 
	type VARCHAR(20) NOT NULL COMMENT 'deposit 押金 / rent 租金', 
	period VARCHAR(20) NOT NULL, 
	amount NUMERIC(10, 2) NOT NULL, 
	due_date DATETIME NOT NULL, 
	status VARCHAR(20) NOT NULL COMMENT 'unpaid 未缴 / paid 已缴 / cancelled 已作废', 
	paid_at DATETIME, 
	remark VARCHAR(200) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (bill_no), 
	FOREIGN KEY(contract_id) REFERENCES contracts (id)
) COMMENT='账单 — 合同生效时一次排满整期：1 条押金 + N 条租金' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_bills_contract_id ON bills (contract_id);

CREATE INDEX ix_bills_due_date ON bills (due_date);

CREATE INDEX ix_bills_status ON bills (status);

CREATE INDEX ix_bills_type ON bills (type);

CREATE TABLE chat_lines (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	conversation_id INTEGER NOT NULL, 
	sender_id INTEGER NOT NULL, 
	content TEXT NOT NULL, 
	kind VARCHAR(10) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(conversation_id) REFERENCES conversations (id), 
	FOREIGN KEY(sender_id) REFERENCES users (id)
) COMMENT='会话消息 — conversations 里的一条消息' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_chat_lines_conversation_id ON chat_lines (conversation_id);

CREATE TABLE repair_images (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	repair_id INTEGER NOT NULL, 
	url VARCHAR(255) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(repair_id) REFERENCES repairs (id)
) COMMENT='报修照片 — 单开一表而非给 repairs 加列，见下方说明' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_repair_images_repair_id ON repair_images (repair_id);

CREATE TABLE repair_records (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	repair_id INTEGER NOT NULL COMMENT '唯一约束：一张工单只能结案一次', 
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
) COMMENT='维修记录 — 工单结案时登记的人、物、工时、费用' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE reviews (
	id INTEGER NOT NULL AUTO_INCREMENT, 
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
) COMMENT='租后评价 — 一份合同只能评一次，评价人姓名对外只露姓氏' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_reviews_agent_id ON reviews (agent_id);

CREATE INDEX ix_reviews_house_id ON reviews (house_id);

CREATE TABLE viewing_records (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	appointment_id INTEGER COMMENT '允许为空：临时到访的带看没有对应预约', 
	house_id INTEGER NOT NULL, 
	tenant_id INTEGER NOT NULL, 
	agent_id INTEGER NOT NULL, 
	showed_up BOOL NOT NULL COMMENT '租客是否到场。放鸽子率单列统计——那个数高通常说明预约确认流程有问题，不是中介的服务问题', 
	duration_minutes INTEGER NOT NULL, 
	feedback TEXT NOT NULL, 
	concerns VARCHAR(200) NOT NULL COMMENT '结构化异议，逗号分隔', 
	intention VARCHAR(10) NOT NULL COMMENT 'high 意向强 / medium 一般 / low 意向弱', 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(appointment_id) REFERENCES appointments (id), 
	FOREIGN KEY(house_id) REFERENCES houses (id), 
	FOREIGN KEY(tenant_id) REFERENCES users (id), 
	FOREIGN KEY(agent_id) REFERENCES users (id)
) COMMENT='带看记录 — J-05。不是考勤表，价值在 concerns——同一套房被三个人嫌厨房小，房东就该降价或改描述了。委托解除后不删，问责需要' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_viewing_records_agent_id ON viewing_records (agent_id);

CREATE INDEX ix_viewing_records_appointment_id ON viewing_records (appointment_id);

CREATE INDEX ix_viewing_records_created_at ON viewing_records (created_at);

CREATE INDEX ix_viewing_records_house_id ON viewing_records (house_id);

CREATE INDEX ix_viewing_records_intention ON viewing_records (intention);

CREATE INDEX ix_viewing_records_tenant_id ON viewing_records (tenant_id);

CREATE TABLE payments (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	trade_no VARCHAR(40) NOT NULL COMMENT '唯一，防止同一笔缴费被重复入账', 
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
) COMMENT='缴费流水 — 一条账单对应一条流水，trade_no 唯一防重复入账' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE UNIQUE INDEX ix_payments_bill_id ON payments (bill_id);

CREATE INDEX ix_payments_gateway_trade_no ON payments (gateway_trade_no);

CREATE INDEX ix_payments_status ON payments (status);

CREATE TABLE repair_item_usages (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	record_id INTEGER NOT NULL, 
	item_id INTEGER NOT NULL, 
	item_name VARCHAR(50) NOT NULL, 
	quantity INTEGER NOT NULL, 
	unit_price NUMERIC(10, 2) NOT NULL COMMENT '领用当时的单价快照，物品调价不影响历史', 
	PRIMARY KEY (id), 
	FOREIGN KEY(record_id) REFERENCES repair_records (id), 
	FOREIGN KEY(item_id) REFERENCES repair_items (id)
) COMMENT='物品领用明细 — 单价是领用当时的快照，不跟随物品调价' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_repair_item_usages_item_id ON repair_item_usages (item_id);

CREATE INDEX ix_repair_item_usages_record_id ON repair_item_usages (record_id);

CREATE TABLE refunds (
	id INTEGER NOT NULL AUTO_INCREMENT, 
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
) COMMENT='押金退款流水 — 退租结算的凭据。课程项目内记录模拟退款，不调真实支付渠道——与 D-1、J-06 同一条边界' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_refunds_contract_id ON refunds (contract_id);

CREATE INDEX ix_refunds_status ON refunds (status);
