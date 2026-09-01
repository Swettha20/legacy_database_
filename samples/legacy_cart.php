<?php
$conn = mysqli_connect("localhost", "root", "password", "shop");

function addToCart($productId, $qty) {
    global $conn;
    $sql = "INSERT INTO cart (product_id, quantity) VALUES ($productId, $qty)";
    mysqli_query($conn, $sql);
}
?>