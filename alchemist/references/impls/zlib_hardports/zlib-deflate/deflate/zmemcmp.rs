pub fn zmemcmp(s1: &[u8], s2: &[u8], n: usize) -> i32 {
    for i in 0..n {
        if s1[i] != s2[i] {
            return (s1[i] as i32) - (s2[i] as i32);
        }
    }
    0
}
