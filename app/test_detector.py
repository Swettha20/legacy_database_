from detector import detect_file_type


def test_detects_php():
    content = "<?php echo 'hello'; ?>"
    assert detect_file_type(content) == "php"


def test_detects_java():
    content = "import java.sql.*;\npublic class Foo {}"
    assert detect_file_type(content) == "java"


def test_detects_sql():
    content = "CREATE TABLE users (id INT);"
    assert detect_file_type(content) == "sql"


def test_unknown_for_random_text():
    content = "This is just some plain English text."
    assert detect_file_type(content) == "unknown"


if __name__ == "__main__":
    test_detects_php()
    test_detects_java()
    test_detects_sql()
    test_unknown_for_random_text()
    print("All tests passed!")