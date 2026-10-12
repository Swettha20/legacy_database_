-- Translated from Oracle FUNCTION APPOINTMENT_TOTAL by legacy-db-modernizer
-- Status: OK

CREATE OR REPLACE FUNCTION appointment_total(p_appointment_id INTEGER)
RETURNS NUMERIC AS $$
DECLARE
    v_total NUMERIC := 0;
    rx RECORD;
BEGIN
    FOR rx IN
        SELECT m.unit_price, p.quantity
        FROM prescriptions p
        JOIN medicines m ON m.sku = p.medicine_sku
        WHERE p.appointment_id = p_appointment_id
    LOOP
        v_total := v_total + rx.unit_price * rx.quantity;
    END LOOP;
    RETURN v_total;
END;
$$ LANGUAGE plpgsql;
