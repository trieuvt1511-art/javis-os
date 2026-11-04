# Ghi âm cuộc họp cục bộ

Đường dùng sau khi nạp bản sửa: Chat web -> dấu + -> Cuộc họp. Chọn VI/ES/EN hoặc tự nhận diện, bấm Bắt đầu ghi mic rồi Dừng và lưu; hoặc Chọn audio. Bấm Phiên âm cục bộ, sau đó Tạo biên bản qua model đã kết nối. Không tự mở mic, không ghi khi mở trang hoặc tải lại. Chỉ thu mic, chưa thu âm thanh hệ thống.

Audio lưu thành từng chunk trên đĩa và trong IndexedDB trước khi gửi; POST cùng chunk là idempotent. Khi mất mạng, dừng ghi và giữ phần chưa gửi trong IndexedDB; mở lại Cuộc họp, chọn Chốt phần audio đã lưu. Bản ghi đã dừng tồn tại qua reload. Không xóa audio nguồn khi phiên âm lỗi.

Mỗi brain lưu `meetings/<uuid>/`: status.json, chunk-*, audio.*, normalized.wav, whisper.json, transcript.json/md và minutes.json/md. Không ingest vào sources hoặc tự tạo nhiệm vụ. Trạng thái xử lý bị gián đoạn được đánh dấu lỗi sau restart, có thể thử lại từ audio đã lưu. Phiên âm dài chạy ngoài event loop với timeout; công việc chưa có scheduler bền, nên restart cần bấm thử lại.

Yêu cầu ffmpeg, whisper-cli và model Whisper đa ngôn ngữ `meetings/models/ggml-small.bin`; có thể đặt JAVIS_MEETING_MODEL. Không tự tải model hoặc gửi audio lên cloud khi bấm phiên âm. Module mới độc lập với dictation hiện có. Dùng auth và origin guard của app chính, không mở server mới trong production.

Tóm tắt chỉ gửi transcript văn bản sang model người dùng đang cấu hình; không có tool. Hiện đường text-only API và Claude Code được hỗ trợ, chưa hỗ trợ Grok CLI/Antigravity CLI. Lỗi model hiện trên cùng bản ghi. Biên bản JSON yêu cầu id đoạn cho mọi ý, Markdown dẫn thời gian; thiếu người/hạn ghi chưa xác định. Kiểm id không chứng minh độ đúng ngữ nghĩa, cần người dự họp duyệt.

Kiểm tự động: tests/python/test_meetings.py. Kiểm trình duyệt riêng phải dùng nguồn tổng hợp, không dùng mic thật. Bản sửa chưa phải xác nhận nghiệm thu trên máy người dùng. Không merge khi CI chưa xanh, không coi PR nháp là đã phát hành.
