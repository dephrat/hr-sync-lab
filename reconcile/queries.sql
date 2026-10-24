-- name: overdue_by_department
SELECT COALESCE(w.department, l.department, '(unknown)') AS department,
       COUNT(*) AS overdue
FROM ethena.enrollments en
JOIN ethena.learners l ON l.id = en.learner_id
LEFT JOIN hris.workers w ON lower(w.email) = lower(l.email)
WHERE en.deadline < :now AND en.completion_status NOT IN ('COMPLETED', 'REMOVED')
GROUP BY 1 ORDER BY overdue DESC, department;

-- name: in_hris_not_ethena
SELECT w.id AS worker_id, w.descriptor AS name, w.email, w.department
FROM hris.workers w
WHERE w.active = 1
  AND NOT EXISTS (SELECT 1 FROM ethena.learners l WHERE lower(l.email) = lower(w.email))
  AND NOT EXISTS (SELECT 1 FROM sync.id_map m WHERE m.worker_id = w.id)
ORDER BY w.id;

-- name: in_ethena_not_hris
SELECT l.id AS learner_id, l.name, l.email, l.department
FROM ethena.learners l
WHERE l.status = 'ACTIVE'
  AND NOT EXISTS (SELECT 1 FROM hris.workers w
                  WHERE w.active = 1 AND lower(w.email) = lower(l.email))
ORDER BY l.id;

-- name: terminated_but_active
SELECT DISTINCT w.id AS worker_id, l.id AS learner_id, l.email, w.status_date
FROM hris.workers w
JOIN ethena.learners l
  ON l.id IN (SELECT learner_id FROM sync.id_map WHERE worker_id = w.id)
  OR lower(l.email) = lower(w.email)
WHERE w.terminated = 1 AND l.status = 'ACTIVE'
ORDER BY w.id;

-- name: enrollments_with_audience
SELECT en.id AS enrollment_id, en.learner_id, en.campaign_id, c.campaign_name,
       c.audience, l.is_manager, l.country, l.state, l.custom_category
FROM ethena.enrollments en
JOIN ethena.learners l ON l.id = en.learner_id
JOIN ethena.campaigns c ON c.id = en.campaign_id
ORDER BY en.id;

-- name: queue_age
SELECT id, worker_id, reason,
       ROUND(julianday(:now) - julianday(created_at), 1) AS age_days
FROM sync.review_queue
WHERE status = 'open'
ORDER BY id;
