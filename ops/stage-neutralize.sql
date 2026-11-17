-- Обезвреживание копии боевой базы на stage. Идемпотентно — можно запускать сколько угодно раз.
-- Запуск: psql -X -v ON_ERROR_STOP=1 -v stage_url=https://stage.example.com -d <db> -f stage-neutralize.sql
-- database.secret / database.uuid НЕ меняются (решение владельца 2026-11-11: пусть совпадают с prod).
BEGIN;

-- 1. Адрес stage (от него же зависит защитный cron «[#Email] Проверка на деактивацию почты…»)
UPDATE ir_config_parameter SET value = :'stage_url' WHERE key = 'web.base.url';

-- 2. Почта: выключить серверы и стереть пароли (чтобы случайное включение ничего не отправило)
DO $$ BEGIN
  IF to_regclass('ir_mail_server') IS NOT NULL THEN
    UPDATE ir_mail_server SET active = false, smtp_pass = NULL;
  END IF;
  IF to_regclass('fetchmail_server') IS NOT NULL THEN
    UPDATE fetchmail_server SET active = false, password = NULL;
  END IF;
END $$;

-- 3. Внешние интеграции с общими с prod ключами
DO $$ BEGIN
  IF to_regclass('printnode_account') IS NOT NULL THEN       -- Direct Print: реальные принтеры
    UPDATE printnode_account SET api_key = 'disabled-on-stage';
  END IF;
END $$;
DELETE FROM ir_config_parameter WHERE key = 'zpl_label_designer.api_key';

-- 4. Cron: остаются включёнными только внутренние служебные (если включены и на prod), всё остальное — выключить
UPDATE ir_cron SET active = false
 WHERE active AND cron_name NOT IN (
   'Base: Auto-vacuum internal data',
   'Update user roles',
   'Auto-generate date ranges',
   'Notification: Delete Notifications older than 6 Month',
   'Jobs Garbage Collector',
   'AutoVacuum Job Queue',
   'Refresh Materialized View x_bi_sql_view_output_location_check',
   '[hk]Перевірка що у Account Dashboard sequence 25',
   'Sale Order: Sync Dates to Pickings and MOs'
 );

COMMIT;

-- Отчёт
SELECT 'web.base.url', value FROM ir_config_parameter WHERE key = 'web.base.url'
UNION ALL SELECT 'cron включено', count(*)::text FROM ir_cron WHERE active
UNION ALL SELECT 'почтовых серверов включено', (SELECT count(*) FROM ir_mail_server WHERE active)::text;
