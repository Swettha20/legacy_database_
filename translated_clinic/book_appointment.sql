-- Translated from Oracle PROCEDURE BOOK_APPOINTMENT by legacy-db-modernizer
-- Status: OK
-- REVIEW: String concatenation: Oracle treats NULL as an empty string in || ('a' || NULL = 'a'); Postgres returns NULL. Use concat(a, b) or COALESCE where an operand can be NULL.

CREATE OR REPLACE FUNCTION book_appointment(p_patient_id INTEGER, p_doctor_id INTEGER, p_when TIMESTAMP, OUT p_id INTEGER)
AS $$
DECLARE
    v_busy INTEGER;
BEGIN
    SELECT COUNT(*) INTO STRICT v_busy
    FROM appointments
    WHERE doctor_id = p_doctor_id
      AND starts_at = p_when
      AND status <> 'CANCELLED';

    IF v_busy > 0 THEN
        RAISE EXCEPTION 'Doctor % is already booked at that time', p_doctor_id;
    END IF;

    SELECT COALESCE(MAX(id), 0) + 1 INTO STRICT p_id
    FROM appointments;

    INSERT INTO appointments (id, patient_id, doctor_id, starts_at)
    VALUES (p_id, p_patient_id, p_doctor_id, p_when);
END;
$$ LANGUAGE plpgsql;
