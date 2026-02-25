CREATE DATABASE law_firm;
USE law_firm;

CREATE TABLE User (
    user_id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    password VARCHAR(255) NOT NULL,
    role ENUM('Admin','Advocate') NOT NULL
);

CREATE TABLE Advocate (
    advocate_id INT PRIMARY KEY,
    specialization VARCHAR(100),
    contact VARCHAR(20),
    FOREIGN KEY (advocate_id) REFERENCES User(user_id) ON DELETE CASCADE
);

CREATE TABLE Client (
    client_id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    contact VARCHAR(20),
    address TEXT,
    email VARCHAR(100)
);

CREATE TABLE CaseTable (
    case_id INT AUTO_INCREMENT PRIMARY KEY,
    case_title VARCHAR(200),
    status ENUM('Pending','Closed') DEFAULT 'Pending',
    client_id INT,
    advocate_id INT,
    FOREIGN KEY (client_id) REFERENCES Client(client_id),
    FOREIGN KEY (advocate_id) REFERENCES Advocate(advocate_id)
);

CREATE TABLE Hearing (
    hearing_id INT AUTO_INCREMENT PRIMARY KEY,
    case_id INT,
    hearing_date DATE,
    remarks TEXT,
    next_hearing_date DATE,
    FOREIGN KEY (case_id) REFERENCES CaseTable(case_id)
);

CREATE TABLE Evidence (
    evidence_id INT AUTO_INCREMENT PRIMARY KEY,
    case_id INT,
    document_name VARCHAR(200),
    upload_date DATE,
    file_path VARCHAR(255),
    FOREIGN KEY (case_id) REFERENCES CaseTable(case_id)
);

CREATE VIEW pending_cases AS
SELECT case_id, case_title FROM CaseTable WHERE status='Pending';

CREATE INDEX idx_client_name ON Client(name);