DO $bootstrap$
DECLARE
    admin_user text := current_setting('dial_bootstrap.admin_user');
    admin_schema text := current_setting('dial_bootstrap.admin_schema');
    command text;
BEGIN
    PERFORM pg_advisory_xact_lock(731246019042303);

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = admin_user) THEN
        EXECUTE format('CREATE ROLE %I LOGIN', admin_user);
    END IF;
    EXECUTE format('ALTER ROLE %I PASSWORD NULL', admin_user);
    EXECUTE format('GRANT rds_iam TO %I', admin_user);
    EXECUTE format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO %I', current_database(), admin_user);
    EXECUTE format('ALTER SCHEMA %I OWNER TO %I', admin_schema, admin_user);

    FOR command IN
        SELECT format('ALTER %s %I.%I OWNER TO %I',
            CASE c.relkind WHEN 'r' THEN 'TABLE' WHEN 'p' THEN 'TABLE' WHEN 'S' THEN 'SEQUENCE'
                WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' WHEN 'f' THEN 'FOREIGN TABLE' END,
            n.nspname, c.relname, admin_user)
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = admin_schema AND c.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
          AND (c.relkind <> 'S' OR NOT EXISTS (
                SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
                  AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')))
        ORDER BY CASE c.relkind WHEN 'S' THEN 2 ELSE 1 END, c.relname
    LOOP
        EXECUTE command;
    END LOOP;

    FOR command IN
        SELECT format('ALTER %s %I.%I(%s) OWNER TO %I',
            CASE p.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END,
            n.nspname, p.proname, pg_get_function_identity_arguments(p.oid), admin_user)
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = admin_schema AND p.prokind IN ('f', 'p')
    LOOP
        EXECUTE command;
    END LOOP;

    FOR command IN
        SELECT format('ALTER %s %I.%I OWNER TO %I',
            CASE t.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END, n.nspname, t.typname, admin_user)
        FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = admin_schema AND t.typtype IN ('d', 'e')
    LOOP
        EXECUTE command;
    END LOOP;

    EXECUTE format('GRANT USAGE, CREATE ON SCHEMA %I TO %I', admin_schema, admin_user);
    EXECUTE format('GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA %I TO %I', admin_schema, admin_user);
    EXECUTE format('GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA %I TO %I', admin_schema, admin_user);
    EXECUTE format('GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA %I TO %I', admin_schema, admin_user);
END;
$bootstrap$;
