-- ═══════════════════════════════════════════════════════════
-- 数据库优化清理 SQL — 在 VPS 上执行一次
-- 执行前请先备份数据库
-- ═══════════════════════════════════════════════════════════

-- 1. 删除 books.book_data 中的 chapters 冗余数组
--    （章节数据已正规化存储在 audiobook_chapters 表中）
UPDATE public.books
SET book_data = book_data - 'chapters'
WHERE book_data ? 'chapters';

-- 2. 删除冗余索引
--    idx_audiobook_chapters_book_id 被 PK (book_id, chapter_id) 覆盖
--    idx_audiobook_chapters_audio_url 无查询使用
DROP INDEX IF EXISTS public.idx_audiobook_chapters_book_id;
DROP INDEX IF EXISTS public.idx_audiobook_chapters_audio_url;

-- 3. 回收空间（VACUUM FULL 会锁表，在低峰期执行）
VACUUM FULL public.books;
VACUUM FULL public.audiobook_chapters;

-- 4. 验证
SELECT 'books' AS table_name,
       pg_size_pretty(pg_total_relation_size('public.books')) AS total_size;
SELECT 'audiobook_chapters' AS table_name,
       pg_size_pretty(pg_total_relation_size('public.audiobook_chapters')) AS total_size;
