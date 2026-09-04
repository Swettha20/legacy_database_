import java.sql.*;

public class LegacyOrder {
    public void createOrder(String customerName, double amount) {
        try {
            Connection conn = DriverManager.getConnection(
                "jdbc:mysql://localhost:3306/shop", "root", "password");
            Statement stmt = conn.createStatement();
            String sql = "INSERT INTO orders (customer_name, amount) VALUES ('"
                + customerName + "', " + amount + ")";
            stmt.executeUpdate(sql);
            conn.close();
        } catch (SQLException e) {
            e.printStackTrace();
        }
    }
}