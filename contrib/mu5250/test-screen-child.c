/* Execute the payload's actual child lifecycle logic without invoking UI APIs. */
#include "screen-network.c"
#include <assert.h>
#include <signal.h>

static void launch(int code) {
    child=fork();assert(child>=0);
    if(!child)_exit(code);
    pending_desired=1;error_until=0;
}
static void finish(void) {
    for(int i=0;i<200&&child>0;i++){usleep(10000);reap_child();}
    assert(child==0);
}
int main(void) {
    assert(signal(SIGCHLD,SIG_DFL)!=SIG_ERR);
    launch(0);finish();assert(!error_until);
    launch(17);finish();assert(error_until>0&&pending_desired==-1);
    /* A stock SIGCHLD handler can consume the exact child's exit status. */
    launch(0);assert(waitpid(child,NULL,0)==child);reap_child();assert(child==0&&!error_until);
    /* SIG_IGN is another legal stock policy: no zombie/wait status remains. */
    assert(signal(SIGCHLD,SIG_IGN)!=SIG_ERR);
    launch(0);finish();assert(!error_until);
    puts("4 child lifecycle cases passed (normal, failed, externally reaped, auto-reaped)");
    return 0;
}
