def detect_file_type(content: str) -> str:
    """
    Given raw file content, return a string identifying its type.
    Returns one of: 'java', 'php', 'sql', 'unknown'
    """
    stripped = content.strip()

    # PHP check first - the <?php tag is nearly unambiguous
    if "<?php" in stripped:
        return "php"

    # Java check - look for structural Java keywords
    if "public class" in stripped or "import java." in stripped:
        return "java"

    # SQL check - look for DDL/DML keywords, case-insensitive
    sql_keywords = ["CREATE TABLE", "INSERT INTO", "SELECT ", "ALTER TABLE"]
    upper_content = stripped.upper()
    if any(keyword in upper_content for keyword in sql_keywords):
        return "sql"

    return "unknown"


if __name__ == "__main__":
    # Quick manual test against our sample files
    import os

    samples_dir = "samples"
    for filename in os.listdir(samples_dir):
        filepath = os.path.join(samples_dir, filename)
        with open(filepath, "r") as f:
            content = f.read()
        result = detect_file_type(content)
        print(f"{filename} -> {result}")