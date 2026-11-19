import io
from openpyxl import Workbook, load_workbook

wb = Workbook()
ws = wb.active
c = ws.cell(row=1, column=1)
c.data_type = 's'
c.value = '=HYPERLINK("http://evil.com","Click")'

buf = io.BytesIO()
wb.save(buf)
buf.seek(0)

wb_read = load_workbook(buf, read_only=True, data_only=True)
val = list(wb_read.active.iter_rows(values_only=True))[0][0]
print("Read with read_only=True, data_only=True:", repr(val))
