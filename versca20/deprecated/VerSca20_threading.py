import logging
import sys
import socket
import select
from threading import *
import time

from VerSca20_operator import *

# Create and configure logger
logging.basicConfig(format='%(asctime)s - %(levelname)s: %(message)s', level=logging.DEBUG) #, datefmt='%m/%d/%Y %H:%M:%S %z')
logger = logging.getLogger("sidecar_proxy")
container_to_forward = os.environ['CONTAINER_TO_FORWARD'] #"prime-numbers"

# Changing the buffer_size and delay, you can improve the speed and bandwidth.
# But when buffer get to high or delay go too down, you can broke things
BUFFERSIZE = 4096
DELAY = 0.0001
forward_to = ('127.0.0.1', getContainersPort(container_to_forward)) # Find port number of the service !!!!!!!!!!
PROXY_PORT = 80
TIME_SHORT = 30.0 # Timer to zeroimport logging
TIME_LONG = 90.0
PROXY_ADDR = ('127.0.0.1', PROXY_PORT)


class ResourcesState():
    def __init__(self, cpu_req, cpu_lim, **kwargs):
        self.cpu_req = cpu_req
        self.cpu_lim = cpu_lim

        for key, val in kwargs.items():
            if (key == "mem_req"): self.mem_req = val
            if (key == "mem_lim"): self.mem_lim = val
            if (key == "resp_time"): self.resp_time = val


class Forward:
    def __init__(self):
        self.forward = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    def start(self, host, port):
        try:
            self.forward.connect((host, port))
            return self.forward
        except Exception as e:
            logger.error(e)
            return False

class TheServer:
    """
    Sidecar proxy server with vertical scaling features. Besides performing forwarding proxy functions, it is an event-based 
    CPU core and memory allocation controller that performs scale TO and FROM zero.

    * Scale TO zero: When an app container has not received any request for a period of time, it scales down the resources of
    the app container, so it stays alive but consuming the bare minimum CPU allowed by K8s. This is the "zero" state.
    
    * Scale FROM zero: When an app container is in "zero" state and receives a request, it is scaled up to the resource values
    specified in the Deployment file so the app container can serve the request.

    Args:
        host: Address to which the server listens to
        port: Port to which the server listens to

    Returns:
        Instance of TheServer object
    """
    

    waiting_time_interval = 1 # in seconds
    separator = "____________________________________________________________________________________________________"

    def __init__(self, host, port):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind((host, port))
        self.server.listen(200)
        # Zero state definition (it must be fine tuned for every app)
        #self.zero_state = ResourcesState(cpu_req="10m", cpu_lim="10m", mem_req="10Mi", mem_lim="10Mi", resp_time="1000000m")
        self.zero_state = ResourcesState(cpu_req="10m", cpu_lim="10m")
        self.reqs_in_queue = 0
        self.users_in_sys = 0
        self.clients_req_pending_list = []
        self.reqs_per_client = {}

    def vscale_to_zero(self):
        logger.info(self.separator)
        logger.info("Vertical scale TO zero")
        #verticalScale(cpu_req = self.zero_state.cpu_req, cpu_lim = self.zero_state.cpu_lim, mem_req = self.zero_state.mem_req, mem_lim = self.zero_state.mem_lim)
        verticalScale(self.zero_state.cpu_req, self.zero_state.cpu_lim)
        #updateSLA(self.zero_state.cpu_req, self.zero_state.cpu_lim, self.zero_state.mem_req, self.zero_state.mem_lim, self.zero_state.resp_time)
        logger.info(self.separator)

    def vscale_from_zero(self):
        logger.info(self.separator)
        logger.info("Vertical scale FROM zero")
        [cpu_req, cpu_lim, mem_req, mem_lim] = getDefaultConfigContainer()
        #TODO: Pass default SLA as a dict
        #verticalScale(cpu_req = cpu_req, cpu_lim = cpu_lim, mem_req = mem_req, mem_lim = mem_lim)
        verticalScale(cpu_req, cpu_lim)
        #updateSLA(cpu_req, cpu_lim, mem_req, mem_lim, "100m")
        ctr = 0
        # Wait some time till app container is ready
        while ((isContainerReady() != True)):
            ctr = ctr+1
            logger.info(f"Cycle of {self.waiting_time_interval} secs #: {ctr}")
            time.sleep(self.waiting_time_interval)
        logger.info(self.separator)

    def create_timer(self,time):
        return Timer(time,self.vscale_to_zero)

    def create_and_start_timer(self,time):
        self.t = self.create_timer(time)
        self.t.daemon = True # TODO: Possible way to handle ctrl+C interruption and close proxy w/o sending other request.
        self.t.start()

    def main_loop(self):
        """
        Flow logic of the proxy server. 
        Args: Self
        Returns: Nothing
        """
        # TODO: Introduce logic that makes use of metrics-server API for the TO zero
        self.create_and_start_timer(TIME_SHORT)
        while True:
            ss = select.select
            inputready, outputready, exceptready = ss([self.server], [], [])
            for conn_orig in inputready:
                if conn_orig == self.server:
                    if isInZeroState(self.zero_state):
                        self.vscale_from_zero()
                    self.on_accept() # Attempt to connect client
                    break
    
    def proxy_thread(self, forward, clientsock):
        logger.info(f'{current_thread().name} - ID: {get_ident()}')
        channel = {}
        channel[clientsock] = forward
        channel[forward] = clientsock

        input_list=[self.server, forward, clientsock]
        run_thread = True

        while run_thread:
            time.sleep(DELAY)
            ss = select.select
            inputready, outputready, exceptready = ss(input_list, [], [])
            for conn_orig in inputready:
                if conn_orig == self.server:
                    break
                try:
                    data = conn_orig.recv(BUFFERSIZE)
                except Exception as e:
                    logger.error("Error caused by socket.recv(BUFFERSIZE)")
                    logger.error(e)
                    #break

                # Close connection when no more data is in buffer
                if len(data.decode()) == 0:
                    logger.debug("Empty buffer!")
                    self.on_close(conn_orig, input_list, channel)
                    run_thread = False
                    break
                else:
                    run_thread = self.on_recv(conn_orig, input_list, channel, data)

    def on_accept(self):
        forward = Forward().start(forward_to[0], forward_to[1])
        clientsock, clientaddr = self.server.accept()

        if forward:
            logger.info(self.separator)
            logger.info(f"{clientaddr} has connected")

            thr = Thread(target=self.proxy_thread, args=(forward, clientsock))
            thr.start()
            

        else:
            logger.info("Can't establish connection with remote server.")
            logger.info(f"Closing connection with client side {clientaddr}")
            clientsock.close()

    def on_close(self, conn_orig, input_list, channel):
        #logger.debug(f'On close of: {current_thread().name} - ID: {get_ident()}')
        #logger.debug(f'Socket on_close: {conn_orig}')
        
        conn_orig_remote = conn_orig.getpeername()
        logger.info(f"{conn_orig.getpeername()} has disconnected")
                
        if conn_orig_remote in self.clients_req_pending_list:
            logger.info("Client disconnected had pending requests")
            self.reqs_in_queue = self.reqs_in_queue - self.reqs_per_client[conn_orig_remote]
            self.clients_req_pending_list.remove(conn_orig_remote)
            del self.reqs_per_client[conn_orig_remote]
            self.timer_controlled_by_reqs()

        # remove objects from input_list
        input_list.remove(conn_orig)
        input_list.remove(channel[conn_orig])
        out = channel[conn_orig]
        # close the connection with client
        channel[out].close()  # equivalent to do self.conn_orig.close()
        # close the connection with remote server
        channel[conn_orig].close()
        # delete both objects from channel dict
        del channel[out]
        del channel[conn_orig]
        logger.info(self.separator)

    def on_recv(self, conn_orig, input_list, channel, data):
        #logger.debug(f'On recv of: {current_thread().name} - ID: {get_ident()}')
        #logger.debug(f'Socket on_recv: {conn_orig}')
        #logger.debug(f'Channel[Socket] on_recv: {channel[conn_orig]}')
        run_thread = True
        logger.info(data)

        # Connection destination remote address. If req, then app's addr. If resp, then client addr
        conn_dst_remote = channel[conn_orig].getpeername()
        # Connection destination local address. If req, then random port assigned to proxy. If resp, then PROXY_ADDR
        conn_dst_local =  channel[conn_orig].getsockname()
        # Connection origin local address. If req, then PROXY_ADDR. If resp, then random port assigned to proxy
        conn_orig_local = conn_orig.getsockname()
        # Connection origin remote address. If req, then client addr. If resp, then app's addr
        conn_orig_remote = conn_orig.getpeername()

        # TRANSITIONS
        # Socket obj: For laddr use mySocket.getsockname() and for raddr use mySocket.getpeername()
        # Proxy receiving GET request
        if ((conn_dst_remote == forward_to) and ("GET" in data.decode())):
            self.reqs_in_queue = self.reqs_in_queue + 1
            if conn_orig_remote not in self.reqs_per_client:
                self.reqs_per_client[conn_orig_remote] = 1
            else:
                self.reqs_per_client[conn_orig_remote] += 1
            self.clients_req_pending_list.append(conn_orig_remote)
            #if self.t.is_alive(): self.t.cancel()
            #self.create_and_start_timer(TIME_LONG)
        # Proxy receiving response to a pending request
        if ((conn_dst_local == PROXY_ADDR) and (conn_dst_remote in self.clients_req_pending_list)):
            self.reqs_in_queue = self.reqs_in_queue - 1
            self.reqs_per_client[conn_dst_remote] = self.reqs_per_client[conn_dst_remote] - 1
            self.clients_req_pending_list.remove(conn_dst_remote)

        self.timer_controlled_by_reqs()
        
        try:
            channel[conn_orig].send(data)
        except Exception as e:
            logger.error("Error caused by socket.send(data)")
            logger.error(e)
            self.on_close(conn_orig, input_list, channel)
            run_thread = False

        return run_thread

    def timer_controlled_by_reqs(self):
        # STATES
        if ((self.reqs_in_queue == 0) and (self.t.is_alive() == False)): self.create_and_start_timer(TIME_SHORT)
        if ((self.reqs_in_queue != 0) and self.t.is_alive()): self.t.cancel()
        logger.debug(f"{self.reqs_in_queue} requests in queue...")
        logger.debug(f"Clients with pending requests: {self.clients_req_pending_list}")
      
if __name__ == '__main__':
    server = TheServer('0.0.0.0', PROXY_PORT) # Socket of the Proxy server
    try:
        server.main_loop()
    except KeyboardInterrupt:
        logger.info("Ctrl C - Stopping server")
        #logger.debug(f'Closing app, currently on thread: {current_thread().name} - ID: {get_ident()}')
        #logger.debug(f"{active_count()} active threads")
        #logger.debug(f"List of running threads: {enumerate()}")
        sys.exit(1)