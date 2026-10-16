-- Translated from Oracle FUNCTION DOCTOR_LOAD by legacy-db-modernizer
-- Status: OK

CREATE OR REPLACE FUNCTION doctor_load(p_doctor_id INTEGER)
RETURNS INTEGER AS $$
DECLARE
    v_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO STRICT v_count
    FROM appointments
    WHERE doctor_id = p_doctor_id
      AND status = 'BOOKED';
    RETURN v_count;
END;
$$ LANGUAGE plpgsql;
