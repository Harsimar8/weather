import socket

s = socket.create_connection(("localhost", 5401), timeout=5)
s.settimeout(5)

script_path = "C:/Users/HARSIM~1/DOWNLO~1/FLIGHT~1.7-D/FGDATA~1/Nasal/WXRADA~1.NAS"
command = f'nasal\nio.load_nasal("{script_path}");\n##EOF##\n'

s.sendall(command.encode("utf-8"))

try:
    print("FlightGear response:")
    print(s.recv(4096).decode("utf-8", errors="replace"))
except socket.timeout:
    print("No response received within 5 seconds.")

s.close()
