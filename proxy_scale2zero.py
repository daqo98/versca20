import logging
import sys
import socket
import select
from threading import Timer
import time

from verticalscale_operator import *

# Create and configure logger
logging.basicConfig(format='%(asctime)s - %(levelname)s: %(message)s', level=logging.INFO) #, datefmt='%m/%d/%Y %H:%M:%S %z')
logger = logging.getLogger("sidecar_proxy")

# Changing the buffer_size and delay, you can improve the speed and bandwidth.
# But when buffer get to high or delay go too down, you can broke things
BUFFERSIZE = 4096
DELAY = 0.0001
forward_to = ('localhost', getContainersPort()) # Find port number of the service !!!!!!!!!!
TIME = 30.0 # Timer to zeroimport logging


class ResourcesState():
    def __init__(self, cpu_req, cpu_lim, mem_req, mem_lim):
        self.cpu_req = cpu_req
        self.cpu_lim = cpu_lim
        self.mem_req = mem_req
        self.mem_lim = mem_lim


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
    

    input_list = []
    channel = {}
    waiting_time_interval = 1 # in seconds
    separator = "____________________________________________________________________________________________________"

    def __init__(self, host, port):
        self.s = None
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind((host, port))
        self.server.listen(200)
        self.zeroState = ResourcesState(cpu_req="10m", cpu_lim="10m", mem_req="10Mi", mem_lim="10Mi")

    def vscale_to_zero(self):
        logger.info(self.separator)
        logger.info("Vertical scale TO zero")
        verticalScale(self.zeroState.cpu_req, self.zeroState.cpu_lim, self.zeroState.mem_req, self.zeroState.mem_lim)
        logger.info(self.separator)

    def vscale_from_zero(self):
        logger.info(self.separator)
        logger.info("Vertical scale FROM zero")
        [cpu_req, cpu_lim, mem_req, mem_lim] = getDefaultConfigContainer()
        verticalScale(cpu_req, cpu_lim, mem_req, mem_lim)
        logger.info(self.separator)

    def create_timer(self):
        return Timer(TIME,self.vscale_to_zero)

    def create_and_start_timer(self):
        self.t = self.create_timer()
        #self.t.daemon = True # TODO: Possible way to handle ctrl+C interruption and close proxy w/o sending other request.
        self.t.start()

    def main_loop(self):
        """
        Flow logic of the proxy server. 
        Args: Self
        Returns: Nothing
        """
        # TODO: Introduce logic that makes use of metrics-server API for the TO zero
        # TODO: Analyze timer control. If request processing takes more than TIME secs app could fail. e.g. n>=200K. Might involve to start timer after receiving response.
        self.input_list.append(self.server)
        self.create_and_start_timer()
        while 1:
            time.sleep(DELAY)
            ss = select.select
            inputready, outputready, exceptready = ss(self.input_list, [], [])
            for self.s in inputready:
                if self.s == self.server:
                    self.t.cancel()
                    self.create_and_start_timer()
                    # Perform vertical scaling and wait for container is ready before forwarding the request.
                    # Besides that, stops the timer because container restarting time is unknown.
                    if isInZeroState(self.zeroState):
                        self.t.cancel()
                        restart_count_before_scaling = getContainerRestartCount() # Restart count before re-sizing
                        self.vscale_from_zero()
                        # Wait for container restart - when working w/ a Zero State that doesn't introduce a CrashLoopBackOff warning e.g. cpu="10m", mem="10Mi"
                        ctr = 0
                        # Wait some time till app container is ready and has been restarted (implies it has been resized)
                        while ((isContainerReady() == False) or (getContainerRestartCount() <= restart_count_before_scaling)):
                            ctr = ctr+1
                            logger.info("Cycle of %s secs #: %s" % (self.waiting_time_interval, ctr))
                            time.sleep(self.waiting_time_interval) 
                        self.create_and_start_timer() # Restart the timer after 
                    self.on_accept() # Attempt to forward the request to the app
                    break

                self.data = self.s.recv(BUFFERSIZE)
                # Close connection when no more data is in buffer
                if len(self.data) == 0:
                    self.on_close()
                    break
                else:
                    self.on_recv()

    def on_accept(self):
        forward = Forward().start(forward_to[0], forward_to[1])
        clientsock, clientaddr = self.server.accept()
        if forward:
            logger.info(self.separator)
            logger.info((clientaddr, "has connected"))
            self.input_list.append(clientsock)
            self.input_list.append(forward)
            self.channel[clientsock] = forward
            self.channel[forward] = clientsock
        else:
            logger.info("Can't establish connection with remote server.")
            logger.info(("Closing connection with client side", clientaddr))
            clientsock.close()

    def on_close(self):
        logger.info((self.s.getpeername(), "has disconnected"))
        logger.info(self.separator)
        # remove objects from input_list
        self.input_list.remove(self.s)
        self.input_list.remove(self.channel[self.s])
        out = self.channel[self.s]
        # close the connection with client
        self.channel[out].close()  # equivalent to do self.s.close()
        # close the connection with remote server
        self.channel[self.s].close()
        # delete both objects from channel dict
        del self.channel[out]
        del self.channel[self.s]

    def on_recv(self):
        data = self.data
        logger.info(data)
        self.channel[self.s].send(data)
      
if __name__ == '__main__':
    server = TheServer('0.0.0.0', 80) # Socket of the Proxy server
    try:
        server.main_loop()
    except KeyboardInterrupt:
        logger.info("Ctrl C - Stopping server")
        sys.exit(1)
