-- Translated from Oracle PROCEDURE CANCEL_APPOINTMENT by legacy-db-modernizer
-- Status: OK
-- REVIEW: String concatenation: Oracle treats NULL as an empty string in || ('a' || NULL = 'a'); Postgres returns NULL. Use concat(a, b) or COALESCE where an operand can be NULL.

CREATE OR REPLACE FUNCTION cancel_appointment(p_id INTEGER)
RETURNS void AS $$
DECLARE
    rowcount INTEGER;
    rx RECORD;
BEGIN
    UPDATE appointments
       SET status = 'CANCELLED'
     WHERE id = p_id
       AND status = 'BOOKED';

    GET DIAGNOSTICS rowcount = ROW_COUNT;

    IF rowcount = 0 THEN
        RAISE EXCEPTION 'Appointment % cannot be cancelled', p_id;
    END IF;

    FOR rx IN
        SELECT medicine_sku, quantity
          FROM prescriptions
         WHERE appointment_id = p_id
    LOOP
        UPDATE medicines
           SET stock = stock + rx.quantity
         WHERE sku = rx.medicine_sku;
    END LOOP;
END;
$$ LANGUAGE plpgsql;
