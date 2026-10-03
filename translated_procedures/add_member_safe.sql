-- Translated from Oracle PROCEDURE ADD_MEMBER_SAFE by legacy-db-modernizer
-- Status: OK

CREATE OR REPLACE FUNCTION add_member_safe(p_name VARCHAR, p_email VARCHAR, OUT p_id INTEGER) RETURNS INTEGER AS $$
BEGIN
    INSERT INTO members (name, email) VALUES (p_name, p_email) RETURNING id INTO p_id;
EXCEPTION
    WHEN unique_violation THEN
        SELECT id INTO STRICT p_id FROM members WHERE email = p_email;
END;
$$ LANGUAGE plpgsql;
