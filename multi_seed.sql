UPDATE markets SET seed_yes=seed_yes*60, seed_no=seed_no*60 WHERE kind='manual' AND category='Крипта';
UPDATE markets SET seed_yes=seed_yes*45, seed_no=seed_no*45 WHERE kind='manual' AND category='Спорт';
UPDATE markets SET seed_yes=seed_yes*30, seed_no=seed_no*30 WHERE kind='manual' AND category='Экономика';
UPDATE markets SET seed_yes=seed_yes*35, seed_no=seed_no*35 WHERE kind='manual' AND category='Технологии';
UPDATE markets SET seed_yes=seed_yes*40, seed_no=seed_no*40 WHERE kind='manual' AND category='Развлечения';
UPDATE markets SET seed_yes=seed_yes*15, seed_no=seed_no*15 WHERE kind='manual' AND category='Наука';
UPDATE markets SET seed_yes=seed_yes*12, seed_no=seed_no*12 WHERE kind='manual' AND category='Погода';
UPDATE markets SET seed_yes=seed_yes*8, seed_no=seed_no*8 WHERE kind='manual' AND category='Политика';
INSERT INTO markets (question, category, created_by, created_at, kind, seed_yes, seed_no, options) VALUES
('Франция — Черногория: кто победит?','Футбол ⚽',1950825012,'2026-06-17T00:00:00','multi',0,0,'[{"label":"П1 — Франция","seed":504000},{"label":"Ничья","seed":66000},{"label":"П2 — Черногория","seed":30000}]'),
('Реал — Барселона: кто победит?','Футбол ⚽',1950825012,'2026-06-17T00:00:00','multi',0,0,'[{"label":"П1 — Реал","seed":396000},{"label":"Ничья","seed":234000},{"label":"П2 — Барселона","seed":270000}]'),
('«Зенит» — «Спартак»: кто победит?','Футбол ⚽',1950825012,'2026-06-17T00:00:00','multi',0,0,'[{"label":"П1 — Зенит","seed":188000},{"label":"Ничья","seed":112000},{"label":"П2 — Спартак","seed":100000}]'),
('Аргентина — Бразилия: кто победит?','Футбол ⚽',1950825012,'2026-06-17T00:00:00','multi',0,0,'[{"label":"П1 — Аргентина","seed":280000},{"label":"Ничья","seed":210000},{"label":"П2 — Бразилия","seed":210000}]'),
('Ман Сити — Арсенал: кто победит?','Футбол ⚽',1950825012,'2026-06-17T00:00:00','multi',0,0,'[{"label":"П1 — Ман Сити","seed":325000},{"label":"Ничья","seed":169000},{"label":"П2 — Арсенал","seed":156000}]');
