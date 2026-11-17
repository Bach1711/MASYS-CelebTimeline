# 🔄 QUY TRÌNH LÀM VIỆC VỚI AI (AI DEVELOPMENT WORKFLOW)

Tài liệu này quy định quy trình chuẩn mà AI Agent **bắt buộc phải tuân thủ** khi thực hiện dự án trong thư mục này.

---

## Bước 1: Đọc Spec, Phân tích & Tạo Tickets trên Linear
1. **Nghiên cứu Spec:**
   * AI đọc kỹ file [`SPEC.md`](./SPEC.md) để nắm vững đối tượng, vấn đề, luồng người dùng, giới hạn phạm vi (Out of Scope), định nghĩa hoàn thành (DoD) và Tech Stack của dự án.
2. **Tạo Tickets trên Linear:**
   * Sử dụng tích hợp **Linear MCP**, AI tự động chia nhỏ dự án thành các Tickets (Issues) cụ thể theo từng chức năng/module (ví dụ: *Khởi tạo khung dự án*, *Xây dựng Windows Agent API*, *Tích hợp mở Terminal & Antigravity CLI*, *Giao diện Dashboard & Voice*, *Tích hợp n8n Webhook*...).
   * Mỗi Ticket trên Linear phải có đầy đủ:
     * **Tiêu đề rõ ràng**
     * **Mô tả chi tiết công việc cần làm**
     * **Định nghĩa hoàn thành (Definition of Done / Acceptance Criteria)** riêng cho Ticket đó
     * **Mức độ ưu tiên & Thứ tự thực hiện**
3. **Trình bày với Người dùng:**
   * Sau khi tạo xong trên Linear, AI tổng hợp và trình bày danh sách các Tickets (kèm ID, tiêu đề, tóm tắt và DoD từng ticket) cho người dùng xem xét.

---

## Bước 2: Tạo Bảng Theo Dõi Tiến Độ & Phản Hồi (`tracker.html`)
Ngay sau khi khởi tạo danh sách Tickets ở Bước 1, AI phải tạo một file giao diện quản lý trực quan tên là **`tracker.html`** đặt ngay trong thư mục gốc của dự án (`D:\Quà sinh nhật cho mẹ\pc-remote control\tracker.html`).

### Yêu cầu tính năng của file `tracker.html`:
1. **Theo dõi tiến độ trực quan:**
   * Hiển thị thanh tiến độ tổng thể của dự án (% hoàn thành, số lượng Ticket đang ở trạng thái *Todo*, *In Progress*, *Review*, *Done*).
   * Hiển thị danh sách toàn bộ các Tickets đồng bộ với Linear.
2. **Chi tiết từng Ticket & Định nghĩa hoàn thành (DoD):**
   * Mỗi thẻ Ticket phải hiển thị rõ: **Mã Ticket (Linear ID)**, **Tên Ticket**, **Trạng thái hiện tại**, và danh sách **Checklist Định nghĩa hoàn thành (DoD)** của riêng Ticket đó.
3. **Hệ thống Góp ý (Feedback) 2 cấp độ:**
   * **Góp ý theo từng Ticket (Ticket-level Feedback):** Mỗi Ticket có một khung nhập liệu (Textarea) và trạng thái duyệt (*Đạt / Cần sửa lại*) để người dùng ghi chú lỗi hoặc yêu cầu chỉnh sửa riêng cho tính năng đó.
   * **Góp ý chung toàn dự án (General Feedback):** Một khu vực lớn ở cuối hoặc đầu trang để người dùng ghi các nhận xét tổng thể, thay đổi thiết kế hoặc bổ sung ý tưởng chung cho cả hệ thống.
4. **Cơ chế Lưu để AI đọc được trực tiếp:**
   * Khi người dùng nhập feedback trên `tracker.html` và bấm nút **"💾 Lưu Feedback cho AI"**, trang HTML sẽ xuất/lưu dữ liệu vào file **`feedback.json`** (thông qua File System Access API trên trình duyệt hoặc tải xuống/ghi trực tiếp vào thư mục dự án) và đồng thời cập nhật dữ liệu trong trang.
   * Nhờ đó, khi người dùng nhắn *"Đọc feedback trong file và sửa code giúp tôi"*, AI chỉ cần đọc trực tiếp file `tracker.html` / `feedback.json` để nắm toàn bộ yêu cầu chỉnh sửa mà người dùng không cần gõ lại vào khung chat.

---

## Bước 3: Thảo luận, Chờ lệnh "Duyệt" & Triển khai Code hàng loạt
1. **Cổng phê duyệt (Approval Gate):**
   * **QUY TẮC BẮT BUỘC:** Sau khi hoàn thành Bước 1 (Linear Tickets) và Bước 2 (`tracker.html`), AI **tuyệt đối KHÔNG được tự ý viết code dự án ngay**.
   * AI phải dừng lại để người dùng xem xét danh sách Tickets, mở `tracker.html` kiểm tra và thảo luận (thêm, bớt, sửa đổi phạm vi hoặc DoD của từng Ticket).
2. **Kích hoạt viết code khi có lệnh "Duyệt":**
   * Chỉ khi người dùng xác nhận rõ ràng (ví dụ: nói **"Duyệt"**, **"OK làm đi"**, **"Bắt đầu code"**), AI mới bắt đầu quá trình viết code.
   * Khi đã được duyệt, AI sẽ **thực thi liên tục toàn bộ các Tickets** từ đầu đến cuối theo đúng thứ tự ưu tiên:
     * Trước khi làm Ticket nào: Cập nhật trạng thái Ticket đó sang `In Progress` trên cả **Linear** và **`tracker.html`**.
     * Thực hiện viết code và tự kiểm tra theo đúng **Định nghĩa hoàn thành (DoD)** của Ticket đó.
     * Sau khi hoàn thành Ticket: Đánh dấu hoàn thành các mục DoD và chuyển trạng thái Ticket sang `Done` trên cả **Linear** và **`tracker.html`**.

---

## Bước 4: Vòng lặp Đọc Feedback & Sửa Code (Feedback & Iteration Loop)
1. Sau khi AI code xong (hoặc trong quá trình kiểm thử), người dùng sẽ mở file **`tracker.html`**, kiểm tra từng Ticket đối chiếu với DoD và điền nhận xét vào:
   * Ô **Feedback riêng của từng Ticket** (nếu tính năng đó chưa đúng ý hoặc bị lỗi).
   * Ô **Góp ý chung (General Feedback)** (nếu muốn chỉnh sửa tổng thể).
2. Khi người dùng lưu file và yêu cầu AI kiểm tra:
   * AI truy cập vào file `tracker.html` / `feedback.json`, đọc toàn bộ các góp ý chung và góp ý từng Ticket.
   * AI lập danh sách các điểm cần sửa, tiến hành sửa code tương ứng.
   * Sau khi sửa xong, AI cập nhật lại trạng thái trên `tracker.html` (đánh dấu đã xử lý feedback kèm ghi chú phản hồi của AI) và đồng bộ lên **Linear**.

---

## 📌 Tóm tắt Trạng thái Quy trình (Checklist cho AI)
- [ ] **Giai đoạn 1:** Đọc `SPEC.md` ➔ Tạo Tickets trên **Linear** ➔ Tạo file **`tracker.html`** ➔ Trình bày cho người dùng & **Chờ duyệt**.
- [ ] **Giai đoạn 2:** Nhận lệnh **"Duyệt"** ➔ Viết code cho toàn bộ Tickets ➔ Cập nhật tiến độ thời gian thực lên `tracker.html` & **Linear**.
- [ ] **Giai đoạn 3:** Đọc Feedback của người dùng từ `tracker.html` / `feedback.json` ➔ Sửa code & Cập nhật trạng thái hoàn tất.
