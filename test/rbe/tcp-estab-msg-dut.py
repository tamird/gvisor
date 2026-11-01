"""Extend the clean scratch packet-DUT fixture with an actual recvmsg call."""
from pathlib import Path

def replace(file, old, new):
    p=Path(file);before=p.read_text()
    if before.count(old)!=1:
        raise SystemExit(f'unexpected source shape in {p}')
    p.write_text(before.replace(old,new))

replace("test/packetimpact/dut/posix_server.cc", "#include <sys/types.h>", "#include <sys/types.h>\n#include <sys/uio.h>")
spec="test/packetimpact/proto/posix_server.proto"
replace(spec,"message RecvRequest {\n  int32 sockfd = 1;\n  int32 len = 2;\n  int32 flags = 3;","message RecvRequest {\n  int32 sockfd = 1;\n  int32 len = 2;\n  int32 flags = 3;\n  int32 control_len = 4;")
replace(spec,"message RecvResponse {\n  int32 ret = 1;\n  int32 errno_ = 2;  // \"errno\" may fail to compile in c++.\n  bytes buf = 3;","message RecvResponse {\n  int32 ret = 1;\n  int32 errno_ = 2;  // \"errno\" may fail to compile in c++.\n  bytes buf = 3;\n  int32 msg_flags = 4;\n  bytes control = 5;")
replace("test/packetimpact/dut/posix_server.cc", """    response->set_ret(
        recv(request->sockfd(), buf.data(), buf.size(), request->flags()));""", """    if (request->control_len() != 0) {
      std::vector<unsigned char> control(request->control_len());
      struct sockaddr_storage name = {};
      struct iovec iov = {buf.data(), buf.size()};
      struct msghdr msg = {};
      msg.msg_name = &name;
      msg.msg_namelen = sizeof(name);
      msg.msg_iov = &iov;
      msg.msg_iovlen = 1;
      msg.msg_control = control.data();
      msg.msg_controllen = control.size();
      ssize_t n = recvmsg(request->sockfd(), &msg, request->flags());
      int saved_errno = (n < 0) ? errno : 0;
      response->set_ret(n);
      response->set_errno_(saved_errno);
      if (n >= 0) {
        response->set_buf(buf.data(), n);
        response->set_msg_flags(msg.msg_flags);
        response->set_control(control.data(), msg.msg_controllen);
      }
      return ::grpc::Status::OK;
    }
    response->set_ret(
        recv(request->sockfd(), buf.data(), buf.size(), request->flags()));""")
replace("test/packetimpact/testbench/dut.go", "// SetSockLingerOption sets SO_LINGER", """// RecvErrMessage reads the Linux extended error and ancillary data.
func (dut *DUT) RecvErrMessage(t *testing.T, fd int32) *pb.RecvResponse {
    t.Helper()
    resp, err := dut.posixServer.Recv(context.Background(), &pb.RecvRequest{
        Sockfd: fd, Len: 256, Flags: unix.MSG_ERRQUEUE | unix.MSG_DONTWAIT, ControlLen: 256,
    })
    if err != nil { t.Fatalf("recvmsg request: %v", err) }
    return resp
}

// SetSockLingerOption sets SO_LINGER""")
